"""
SIGEPE Editais — the forward-looking half of the radar.

Every opportunity that appears in SouGov/SIGEPE Oportunidades is created by an
edital, and every published edital is listed on a portal that needs NO login:

    https://oportunidades.sigepe.gov.br/oportunidades-portal/api/html/?p=1

(The Angular app at oportunidades.sigepe.gov.br and its API behind
prod-sigepe-api... are 401/403 without a gov.br token and the right atribuição —
that is the door that stayed shut. This one is server-rendered and open.)

One GET returns the whole corpus — ~2.9k editais since 2021, of which ~60 are
open at any moment — with órgão, número/ano, prazo, the list of unidades/vagas
and the full descriptive text. Per-edital pages give publication date, versions
(retificações) and the PDF:

    /oportunidades-portal/api/html/{id}            detail page
    /oportunidades-portal/api/html/{id}/download   edital PDF

Why this replaces DOU-watching as the primary signal: the DOU tells you who
*took* a post after the fact. An edital tells you a post is open and how to
apply, before it is filled.

Usage:
    python sigepe_editais.py                 # open editais, scored, -> EDITAIS.md
    python sigepe_editais.py --all           # include the ones below the cut
    python sigepe_editais.py --min-score 4
    python sigepe_editais.py --history "coordenador-geral"   # search the archive
    python sigepe_editais.py --closed        # include already-closed editais
"""
import re
import csv
import sys
import json
import argparse
import datetime
from pathlib import Path

import requests

PORTAL = 'https://oportunidades.sigepe.gov.br/oportunidades-portal/api/html'
LISTING = f'{PORTAL}/?p=1'   # any query param; the bare path 500s
DATA_DIR = Path(__file__).parent / 'data'
CSV_PATH = DATA_DIR / 'editais.csv'

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml',
}

# --- Profile configuration -------------------------------------------------
# Bernardo Furtado: TPP/IPEA, Brasília. Looking for a cessão into a leadership
# post where modelling / policy evaluation is the actual job.

HOME_UF = 'DF'

# The cargo actually on offer, by how much it justifies leaving IPEA.
# Order matters: first match wins, so the strongest patterns come first.
CARGO_LEVELS = [
    (6, 'secretário/diretor',
     r'\b(secretári[oa]\s+(?:nacional|de|adjunt)|'
     r'diretor(?:a)?(?:\s|-|$)|superintendent|'
     r'(?:CCE|FCE)\s*[123]\.1[5-9]|DAS\s*10[12]\.[56])'),
    (5, 'coordenador-geral',
     r'\b(coordenador(?:a)?-geral|coordenador(?:a)?\s+geral|'
     r'(?:CCE|FCE)\s*[123]\.1[34]|DAS\s*10[12]\.4|'
     r'assessor(?:a)?\s+especial|chefe\s+de\s+gabinete)'),
    (1, 'coordenador (1.10)',
     r'\b(coordenador(?:a)?\b|assessor(?:a)?\b|'
     r'(?:CCE|FCE)\s*[123]\.1[012]|DAS\s*10[12]\.3)'),
    (-3, 'chefia baixa',
     r'\b(chefe\s+d[eoa]\s+(?:divisão|divisao|serviço|servico|setor|seção|secao)|'
     r'(?:CCE|FCE)\s*[123]\.0[1-9]|DAS\s*10[12]\.[12])'),
]

# A unit name is not a job title. "GSISTE na Coordenação-Geral de X" means you
# would work *inside* a CG, not run one — the single biggest false positive when
# scoring these titles, so strip unit names before looking for a cargo.
UNIT_NOISE = re.compile(
    r'\b(coordena(?:ção|cao)(?:-geral)?|diretoria|secretaria|superintend(?:ência|encia)|'
    r'gabinete|departamento|gerência|gerencia|assessoria)\b[^,;|\n]{0,80}', re.I)

# Gratificações are bonuses attached to technical staffing, not leadership, and
# most are gated on an IT/careers profile this user does not have.
GRATIFICACAO = re.compile(r'\b(GSISP|GSISTE|GAEG|GSIST|gratifica(?:ção|cao)\s+tempor)', re.I)

# Explicitly out: level and formation the profile cannot meet.
DISQUALIFIERS = [
    (r'n[íi]vel\s+(?:m[ée]dio|intermedi[áa]rio)|\bNI\b|\(NI\)', 'nível médio/intermediário'),
    (r'gradua(?:ção|cao)\s+em\s+(?!ci[êe]ncias?\s+(?:econ|sociais)|econom|estat[íi]st|'
     r'geografia|arquitetura|urbanis|administra|qualquer)', 'graduação específica'),
    (r'\bmedicina\b|\benfermagem\b|\bodontolog', 'área da saúde'),
]

# Expertise tiers — kept in sync with career_radar by import when available.
try:
    from career_radar import (EXPERTISE_TIER1, EXPERTISE_TIER2, EXPERTISE_TIER3,
                              TARGET_ORGANS)
except ImportError:  # standalone
    EXPERTISE_TIER1 = EXPERTISE_TIER2 = EXPERTISE_TIER3 = []
    TARGET_ORGANS = []

UFS = ('AC AL AP AM BA CE ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC SP SE TO DF')
UF_PATTERN = re.compile(r'\b([A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wÁ-ÿ\.\- ]{2,28})/(' +
                        '|'.join(UFS.split()) + r')\b')


# --- Fetch & parse ---------------------------------------------------------

def fetch_listing(timeout=120, attempts=4):
    """Download the full public edital listing (~8 MB of server-rendered HTML).

    The portal renders all ~2.9k editais on every request and intermittently
    answers 500 under that load — retrying the identical URL succeeds. Observed
    roughly one failure in six, so this is expected, not a signal of breakage.
    """
    import time
    last = None
    for i in range(attempts):
        try:
            r = requests.get(LISTING, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            r.encoding = 'utf-8'
            return r.text
        except requests.RequestException as e:
            last = e
            if i < attempts - 1:
                time.sleep(3 * (i + 1))
    raise last


HEAD_RE = re.compile(r'^(.*?)\s+-\s+Edital\s+N°?:\s*([\d.]+)/(\d{4})\s*$')


def parse_listing(html):
    """Turn the listing page into one record per edital.

    Structure (verified 2026-08-21): #editais holds a flat sequence of <div
    class="row">. A row containing <a onclick="...+ID..."> starts a new edital;
    the rows that follow it, until the next anchor, carry its unidades
    (span.br-tag) and descriptive text.
    """
    from bs4 import BeautifulSoup
    html = re.sub(r'<style.*?</style>', '', html, flags=re.S)
    soup = BeautifulSoup(html, 'html.parser')
    root = soup.select_one('#editais')
    if root is None:
        return []

    today = datetime.date.today()
    records, cur = [], None
    for el in root.children:
        if getattr(el, 'name', None) != 'div':
            continue
        anchor = el.select_one('a[onclick]')
        if anchor is not None:
            m = re.search(r'\+(\d+)', anchor.get('onclick', ''))
            head = re.sub(r'\s+', ' ', anchor.get_text(strip=True))
            hm = HEAD_RE.match(head)
            tag = el.select_one('span.br-tag')
            status = tag.get_text(strip=True) if tag else ''
            cur = {
                'id': m.group(1) if m else '',
                'orgao': hm.group(1) if hm else head,
                'numero': f'{hm.group(2)}/{hm.group(3)}' if hm else '',
                'ano': hm.group(3) if hm else '',
                'status': status,
                'state': _state(status),
                'deadline': _deadline(status, today),
                'opps': [],
                'desc': '',
            }
            records.append(cur)
        elif cur is not None and not cur['opps'] and not cur['desc']:
            cur['opps'] = [s.get_text(strip=True) for s in el.select('span.br-tag')]
            cur['desc'] = re.sub(r'\s+', ' ', el.get_text(' ', strip=True))
    return records


def _state(status):
    if status.startswith('Inscrições Encerradas'):
        return 'encerrado'
    if status.startswith('Início'):
        return 'a abrir'
    if status.startswith('Encerra'):
        return 'aberto'
    return 'desconhecido'


def _deadline(status, today):
    m = re.search(r'Encerra em:\s*(\d+)\s*dia', status)
    if m:
        return (today + datetime.timedelta(days=int(m.group(1)))).strftime('%d/%m/%Y')
    m = re.search(r'(\d{2}/\d{2}/\d{4})', status)
    return m.group(1) if m else ''


def edital_url(rec):
    return f'{PORTAL}/{rec["id"]}'


def pdf_url(rec):
    return f'{PORTAL}/{rec["id"]}/download'


# --- Scoring ---------------------------------------------------------------

def cargo_level(rec):
    """(points, label) for the cargo actually being offered.

    Unit names are removed first: 'GSISTE na Coordenação-Geral de Dados' offers
    no cargo at all, and scoring it as a coordenação-geral was the whole reason
    the DOU version kept surfacing irrelevant hits.
    """
    titles = ' | '.join(rec['opps']) or rec['desc'][:300]
    stripped = UNIT_NOISE.sub(' ', titles)
    # An explicit CCE/FCE/DAS code survives unit-stripping and is authoritative.
    for pts, label, pat in CARGO_LEVELS:
        if re.search(pat, stripped, re.I):
            return pts, label
    for pts, label, pat in CARGO_LEVELS:
        if re.search(r'(?:CCE|FCE|DAS)\s*[\d.]+', titles, re.I) and \
                re.search(pat, titles, re.I):
            return pts, label
    return 0, ''


def locality(rec):
    """(points, place) — a post outside the DF is not a move this profile makes."""
    text = f'{" | ".join(rec["opps"])} {rec["desc"][:1200]}'
    if re.search(r'bras[íi]lia|\bDF\b|distrito federal', text, re.I):
        return 1, 'Brasília'
    hits = [m for m in UF_PATTERN.finditer(text) if m.group(2) != HOME_UF]
    if hits:
        return -5, f'{hits[0].group(1).strip()}/{hits[0].group(2)}'
    return 0, ''


def score_expertise(text):
    """Tiered regex match — see career_radar.EXPERTISE_TIER1 for why regex."""
    score, matched = 0, []
    for tier, pts in ((EXPERTISE_TIER1, 3), (EXPERTISE_TIER2, 2), (EXPERTISE_TIER3, 1)):
        for pat, label in tier:
            if re.search(pat, text, re.IGNORECASE):
                score += pts
                matched.append(f'{label}(+{pts})')
    return score, matched


def classify(rec):
    """Score an edital against the profile. Returns a dict merged into the record."""
    text = f'{rec["orgao"]} {" | ".join(rec["opps"])} {rec["desc"]}'.lower()
    score, why = 0, []

    pts, label = cargo_level(rec)
    if pts:
        score += pts
        why.append(f'{label}({pts:+d})')

    pts, place = locality(rec)
    if pts:
        score += pts
        why.append(f'{place}({pts:+d})')

    exp, matches = score_expertise(text)
    score += exp
    why += matches

    if any(o in text for o in TARGET_ORGANS):
        score += 2
        why.append('órgão-alvo(+2)')

    if GRATIFICACAO.search(text):
        score -= 3
        why.append('gratificação técnica(-3)')

    blockers = [name for pat, name in DISQUALIFIERS if re.search(pat, text, re.I)]
    if blockers:
        score -= 4
        why.append(f'{blockers[0]}(-4)')

    rec['score'] = score
    rec['level'] = label
    rec['place'] = place
    rec['why'] = ', '.join(why)
    rec['blockers'] = '; '.join(blockers)
    return rec


# --- Persistence -----------------------------------------------------------

CSV_FIELDS = ['id', 'numero', 'ano', 'orgao', 'state', 'status', 'deadline',
              'score', 'level', 'place', 'why', 'blockers', 'opps', 'url',
              'first_seen', 'last_seen']


def scan():
    """Fetch, parse, score and persist. Returns (records, new_ids)."""
    records = parse_listing(fetch_listing())
    for r in records:
        classify(r)
    return records, save(records)


def load_open(days=7):
    """Open/opening editais from data/editais.csv, newest-first by score.

    The digest reads the CSV rather than re-fetching: the daily scan already
    wrote it, and first_seen is what makes "novo esta semana" mean anything.
    """
    cutoff = datetime.date.today() - datetime.timedelta(days=days)
    rows = []
    for row in load_seen().values():
        if row['state'] not in ('aberto', 'a abrir'):
            continue
        try:
            row['score'] = int(row['score'])
        except (TypeError, ValueError):
            row['score'] = 0
        try:
            first = datetime.datetime.strptime(row['first_seen'], '%d/%m/%Y').date()
        except (TypeError, ValueError):
            first = cutoff
        row['is_new'] = first >= cutoff
        row['opps'] = [o for o in row['opps'].split(' | ') if o]
        rows.append(row)
    rows.sort(key=lambda r: (-r['score'], r['deadline']))
    return rows


def load_seen():
    if not CSV_PATH.exists():
        return {}
    with open(CSV_PATH, encoding='utf-8', newline='') as fh:
        return {row['id']: row for row in csv.DictReader(fh, delimiter=';')}


def save(records):
    """Persist every edital, preserving first_seen. Returns the ids new this run."""
    DATA_DIR.mkdir(exist_ok=True)
    today = datetime.date.today().strftime('%d/%m/%Y')
    seen = load_seen()
    new_ids = []
    for r in records:
        prior = seen.get(r['id'])
        if prior and prior['state'] == 'encerrado' == r['state']:
            continue  # settled history — leave the row byte-identical
        row = {
            'id': r['id'], 'numero': r['numero'], 'ano': r['ano'],
            'orgao': r['orgao'], 'state': r['state'], 'status': r['status'],
            'deadline': r['deadline'], 'score': r.get('score', 0),
            'level': r.get('level', ''), 'place': r.get('place', ''),
            'why': r.get('why', ''), 'blockers': r.get('blockers', ''),
            'opps': ' | '.join(r['opps'])[:400], 'url': edital_url(r),
            'first_seen': prior['first_seen'] if prior else today,
            'last_seen': today,
        }
        if not prior:
            new_ids.append(r['id'])
        seen[r['id']] = row
    with open(CSV_PATH, 'w', encoding='utf-8', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, delimiter=';',
                           lineterminator='\n')  # \r\n would make git renormalise every row
        w.writeheader()
        for row in sorted(seen.values(), key=lambda x: -int(x['id'] or 0)):
            w.writerow(row)
    return new_ids


# --- Output ----------------------------------------------------------------

def _row(r, new_ids=()):
    flag = ' 🆕' if (r.get('is_new') or r['id'] in new_ids) else ''
    opps = ' / '.join(r['opps'])[:110] or '—'
    return (f'| **{r["score"]}** | [{r["numero"]}]({edital_url(r)}){flag} '
            f'| {r["orgao"][:46]} | {opps} | {r["deadline"] or r["status"]} '
            f'| {r["why"]} |')


HEAD = ('| Sc | Edital | Órgão | Vaga | Prazo | Por quê |\n'
        '|---:|--------|-------|------|-------|---------|')


def digest(records, new_ids=(), min_score=4, show_all=False):
    today = datetime.date.today().strftime('%d/%m/%Y')
    openish = [r for r in records if r['state'] in ('aberto', 'a abrir')]
    openish.sort(key=lambda r: (-r['score'], r['deadline']))
    top = [r for r in openish if r['score'] >= min_score]
    rest = [r for r in openish if r['score'] < min_score]

    lines = [f'# SIGEPE Editais — {today}', '',
             f'{len(openish)} editais abertos ou abrindo. '
             f'{len(top)} passam do corte (score >= {min_score}). '
             f'{sum(1 for r in openish if r["id"] in new_ids)} novos desde a última varredura.',
             '']
    if top:
        lines += ['## Vale olhar', '', HEAD]
        lines += [_row(r, new_ids) for r in top]
        lines.append('')
    else:
        lines += ['*Nenhum edital acima do corte esta semana.*', '']

    if show_all and rest:
        lines += [f'<details><summary>Outros {len(rest)} editais abertos</summary>',
                  '', HEAD]
        lines += [_row(r, new_ids) for r in rest]
        lines += ['', '</details>', '']

    lines += ['---',
              '*Score: cargo (+6 diretor/secretário, +5 coord-geral, +1 coordenador 1.10, '
              '−3 chefia baixa) · lotação (+1 DF, −5 fora) · tema (+3/+2/+1) · '
              'órgão-alvo (+2) · gratificação técnica (−3) · requisito bloqueante (−4).*',
              '',
              '*Fonte: [Portal de Editais de Oportunidades]'
              '(https://oportunidades.sigepe.gov.br/oportunidades-portal/api/html/?p=1) '
              '— público, sem login. Cada link abre o edital; `/download` traz o PDF.*']
    return '\n'.join(lines)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--min-score', type=int, default=4)
    ap.add_argument('--all', action='store_true',
                    help='also list open editais below the cut')
    ap.add_argument('--closed', action='store_true',
                    help='score closed editais too (calibration)')
    ap.add_argument('--history', metavar='REGEX',
                    help='search the whole archive and print matches')
    ap.add_argument('--out', default='EDITAIS.md')
    ap.add_argument('--json', metavar='PATH', help='dump parsed records as JSON')
    args = ap.parse_args()

    print('Baixando o portal público de editais...', file=sys.stderr)
    records = parse_listing(fetch_listing())
    print(f'  {len(records)} editais no portal', file=sys.stderr)
    if not records:
        print('Nada parseado — o layout do portal pode ter mudado.', file=sys.stderr)
        return 1

    for r in records:
        classify(r)

    if args.json:
        Path(args.json).write_text(json.dumps(records, ensure_ascii=False, indent=1),
                                   encoding='utf-8')

    if args.history:
        pat = re.compile(args.history, re.I)
        hits = [r for r in records
                if pat.search(f'{r["orgao"]} {" ".join(r["opps"])} {r["desc"]}')]
        hits.sort(key=lambda r: -r['score'])
        print(f'{len(hits)} editais casam com /{args.history}/\n')
        print(HEAD)
        for r in hits[:80]:
            print(_row(r))
        return 0

    new_ids = save(records)
    print(f'  {len(new_ids)} novos', file=sys.stderr)

    out = digest(records, new_ids, args.min_score, show_all=args.all)
    Path(args.out).write_text(out + '\n', encoding='utf-8')
    print(out)
    print(f'\n-> {args.out}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
