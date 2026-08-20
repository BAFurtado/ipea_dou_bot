"""
Career Radar — monitors the DOU (Diário Oficial da União) for:
1. IPEA personnel changes (original bot functionality)
2. Vacancy signals: exonerações/dispensas of DAS/CCE/FCE positions at target organs
3. Opportunity signals: editais, processos seletivos in relevant areas

Uses requests + BeautifulSoup instead of Selenium for speed and reliability.
Output: CSV data files + email-ready digest in README.md
"""
import os
import re
import csv
import json
import datetime
import pandas as pd
import requests
from pathlib import Path

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

DATA_DIR = Path(__file__).parent / 'data'

# --- Search configuration ---

# Original IPEA tracking
IPEA_QUERIES = [
    {'query': 'ipea', 'secao': 2, 'label': 'IPEA Pessoal'},
]

# Career radar: vacancy signals — DOU Section 2
#
# NOTE ON QUERY SYNTAX (verified live 2026-08-20):
# The in.gov.br search honours ONE quoted phrase, or several unquoted terms.
# It returns ZERO for two quoted phrases combined, and ZERO for `OR`.
# e.g.  dispensar+"coordenador-geral"        -> 0 hits
#       dispensar+coordenador-geral          -> 20 hits
#       "processo+seletivo"+"avaliação"      -> 0 hits
#       "processo+seletivo+simplificado"     -> 20 hits
# Every query below was probed against the live endpoint and returns results.
# Do not reintroduce multi-phrase or OR syntax without re-probing.
VACANCY_QUERIES = [
    # Broad vacancy signals at any organ
    {'query': 'exonerar+CCE', 'secao': 2, 'label': 'Exoneração CCE'},
    {'query': 'exonerar+FCE', 'secao': 2, 'label': 'Exoneração FCE'},
    {'query': 'exonerar+coordenador-geral', 'secao': 2, 'label': 'Exoneração Coord-Geral'},
    {'query': 'dispensar+coordenador-geral', 'secao': 2, 'label': 'Dispensa Coord-Geral'},
    {'query': 'dispensar+diretor', 'secao': 2, 'label': 'Dispensa Diretor'},
    # Domain-targeted: single quoted phrase only
    {'query': '"avaliação+de+políticas"', 'secao': 2, 'label': 'Avaliação Políticas'},
    {'query': '"monitoramento+e+avaliação"', 'secao': 2, 'label': 'Monitoramento e Avaliação'},
    {'query': '"ciência+de+dados"', 'secao': 2, 'label': 'Ciência de Dados'},
    {'query': '"inteligência+artificial"', 'secao': 2, 'label': 'IA'},
    {'query': 'modelagem', 'secao': 2, 'label': 'Modelagem'},
]

# Opportunity signals: selection processes, editais, chamamentos.
# These run across Sections 1, 2 and 3 — editais surface in all three.
OPPORTUNITY_QUERIES = [
    {'query': '"processo+seletivo+simplificado"', 'secao': 3, 'label': 'PSS (S3)'},
    {'query': '"processo+seletivo+simplificado"', 'secao': 2, 'label': 'PSS (S2)'},
    {'query': '"processo+seletivo+simplificado"', 'secao': 1, 'label': 'PSS (S1)'},
    {'query': '"edital+de+seleção"', 'secao': 3, 'label': 'Edital de Seleção'},
    {'query': '"edital+de+chamamento"', 'secao': 1, 'label': 'Edital de Chamamento'},
    {'query': '"seleção+simplificada"', 'secao': 3, 'label': 'Seleção Simplificada'},
    {'query': '"chamada+pública"', 'secao': 3, 'label': 'Chamada Pública'},
]

# Organs to EXCLUDE — not relevant for career search
EXCLUDED_ORGANS = [
    # Level-1 hierarchy buckets — these were the 35% of stored noise:
    # Poder Legislativo (26), Poder Judiciário (16), conselhos (13), TCU, Defesa, MRE.
    'poder legislativo', 'poder judiciário', 'poder judiciario',
    'entidades de fiscalização', 'entidades de fiscalizacao',
    'conselho regional', 'conselho federal', 'ordem dos advogados',
    'tribunal de contas',
    'ministério da defesa', 'ministério das relações exteriores',
    'senado federal', 'câmara dos deputados',
    # Military / police
    'comando do exército', 'comando da marinha', 'comando da aeronáutica',
    'comando militar', 'estado-maior', 'força aérea',
    'polícia federal', 'polícia rodoviária',
    # Education / academia — not cessão targets for this profile
    'instituto federal de educação', 'universidade federal', 'universidade da',
    'fundação universidade', 'colégio pedro ii',
    'centro federal de educação tecnológica',
    'hospital universitário', 'empresa brasileira de serviços hospitalares',
    # Courts / prosecution
    'tribunal regional', 'tribunal de justiça', 'tribunal superior',
    'justiça federal', 'justiça do trabalho',
    'ministério público', 'procuradoria', 'defensoria',
    # Subnational — a federal servidor cannot be ceded into these
    'prefeitura', 'governo do estado', 'câmara municipal',
    'governo do distrito federal', 'assembleia legislativa',
    # Operational agencies
    'receita federal',
    'instituto nacional do seguro social',
    'agência nacional de vigilância',
    'departamento nacional de infraestrutura',
    'superintendência regional',
]

# Target organs where profile fits — Brasília-based executive branch
TARGET_ORGANS = [
    'ministério das cidades',
    'ministério do planejamento',
    'ministério da gestão',
    'casa civil',
    'enap',
    'escola nacional de administração pública',
    'ibge',
    'instituto brasileiro de geografia',
    'controladoria-geral da união',
    'ministério da ciência',
    'ministério do meio ambiente',
    'ministério do desenvolvimento',
    'ministério da fazenda',
    'secretaria do tesouro nacional',
    'bndes',
    'cade',
    'ipea',
    'fundação instituto de pesquisa econômica',
    'ministério da justiça',
    'ministério da igualdade racial',
    'presidência da república',
    'casa civil da presidência',
    'ministério do trabalho',
    'ministério da saúde',
    'ministério da previdência',
    'ministério da educação',
    'ministério dos transportes',
    'ministério da integração',
    'ministério do turismo',
]

# Minimum position level: coordenador-geral (1.13) and above.
# Coordenador simples (1.10) does NOT allow cessão from IPEA.
POSITION_KEYWORDS = [
    'coordenador-geral', 'diretor', 'assessor especial', 'secretário',
    'CCE 1.13', 'CCE 1.14', 'CCE 1.15', 'CCE 1.17',
    'CCE 2.13', 'CCE 2.14', 'CCE 2.15',
    'CCE 3.13', 'CCE 3.15',
    'FCE 1.13', 'FCE 1.14', 'FCE 1.15',
    'DAS 101.4', 'DAS 101.5', 'DAS 101.6',
    'DAS 102.4', 'DAS 102.5', 'DAS 102.6',
]

# Positions too low to justify a cessão — filter these out
LOW_POSITIONS = [
    'CCE 1.10', 'CCE 1.07', 'CCE 1.05',
    'FCE 1.10', 'FCE 1.07', 'FCE 1.05',
    'DAS 101.1', 'DAS 101.2', 'DAS 101.3',
    'DAS 102.1', 'DAS 102.2', 'DAS 102.3',
    'FG', 'CD-',
    'chefe de divisão', 'chefe de setor', 'chefe de seção',
    'chefe da divisão', 'chefe do setor',
]

# Keywords weighted by how closely they match the user's actual expertise.
# Tier 1 (+3): core — modeling/evaluation applied to policy, the actual draw
EXPERTISE_TIER1 = [
    'modelagem', 'simulação', 'cenários', 'avaliação ex-ante',
    'avaliação de impacto', 'contrafactual', 'sistemas complexos',
    'agentes', 'agent-based', 'econometria espacial',
    'evidências', 'política baseada em evidências',
]
# Tier 2 (+2): domain areas where modeling expertise applies directly
EXPERTISE_TIER2 = [
    'urbano', 'urbana', 'habitação', 'habitacional', 'metropolitano',
    'metrópoles', 'espacial', 'infraestrutura',
    'avaliação de políticas', 'monitoramento e avaliação',
    'planejamento estratégico', 'transição energética',
    'desenvolvimento sustentável',
]
# Tier 3 (+1): supporting skills — relevant but not the core draw
EXPERTISE_TIER3 = [
    'ciência de dados', 'inteligência artificial',
    'geoprocessamento', 'pesquisa aplicada',
    'desenvolvimento regional', 'políticas públicas',
    'meio ambiente',
]


def search_dou(query, secao=2, period='dia'):
    """Search DOU and return parsed JSON results."""
    url = (f'https://www.in.gov.br/consulta/-/buscar/dou?'
           f'q={query}&s=do{secao}&exactDate={period}&sortType=0')
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f'  Request failed for {query}: {e}')
        return []

    match = re.search(
        r'type="application/json">\s*(\{.*?\})\s*</',
        r.text, re.DOTALL
    )
    if not match:
        return []

    raw = match.group(1)
    raw = raw.replace("<span class='highlight' style='background:#FFA;'>", "")
    raw = raw.replace("</span>", "").replace("<\\/span>", "")
    raw = raw.replace('\\/', '/')

    try:
        data = json.loads(raw)
        return data.get('jsonArray', [])
    except json.JSONDecodeError:
        return []


def clean_html(text):
    """Remove HTML tags from text."""
    return re.sub(r'<[^>]+>', '', text) if text else ''


def is_excluded_organ(item):
    """Check if this result is from an irrelevant organ (military, police, universities, etc.)."""
    hierarchy = [h.lower() for h in item.get('hierarchyList', [])]
    organ_str = ' '.join(hierarchy)
    return any(exc in organ_str for exc in EXCLUDED_ORGANS)


# Sections 1 and 3 are dominated by purchasing, not hiring. These phrases mark a
# procurement document, which no amount of expertise scoring makes relevant.
PROCUREMENT_NOISE = [
    'extrato de contrato', 'extrato de termo aditivo', 'termo aditivo',
    'extrato de dispensa', 'extrato de inexigibilidade', 'extrato de convênio',
    'resultado de julgamento', 'aviso de licitação', 'aviso de homologação',
    'ata de registro de preços', 'pregão eletrônico', 'tomada de preços',
    'concorrência pública', 'extrato de rescisão', 'aviso de anulação',
    'aviso de suspensão', 'extrato de acordo', 'extrato de instrumento',
    'credenciamento de leiloeiro', 'chamamento público para aquisição',
]


def is_procurement(text):
    """True when the document is about buying something, not filling a post."""
    return any(p in text for p in PROCUREMENT_NOISE)


def is_noise(text):
    """Detect noise: procurement, substituto, exonerar+nomear pairs, low positions."""
    text_lower = text.lower()
    if is_procurement(text_lower):
        return True
    # Substituto eventual — not a real opening
    substituto_noise = [
        'substitut', 'responder pelas atribuições',
        'afastamentos e impedimentos legais',
        'impedimentos legais ou regulamentares',
    ]
    if any(n in text_lower for n in substituto_noise):
        return True
    # Exonerar + nomear in same text = position was filled immediately
    has_exonerar = any(w in text_lower for w in ['exonerar', 'exoneração'])
    has_nomear = any(w in text_lower for w in ['nomear', 'nomeação', 'designar'])
    if has_exonerar and has_nomear:
        return True
    # Position too low for cessão (< coordenador-geral / 1.13)
    has_high = any(kw.lower() in text_lower for kw in POSITION_KEYWORDS)
    has_low = any(kw.lower() in text_lower for kw in LOW_POSITIONS)
    if has_low and not has_high:
        return True
    return False


def score_expertise(text):
    """Score expertise match using tiered keywords. Returns (score, matched_keywords)."""
    matched = []
    score = 0
    for kw in EXPERTISE_TIER1:
        if kw in text:
            score += 3
            matched.append(f'{kw}(+3)')
    for kw in EXPERTISE_TIER2:
        if kw in text:
            score += 2
            matched.append(f'{kw}(+2)')
    for kw in EXPERTISE_TIER3:
        if kw in text:
            score += 1
            matched.append(f'{kw}(+1)')
    return score, matched


# A bare role word matches the person SIGNING the portaria as often as the vacancy:
# "O DIRETOR DO DEPARTAMENTO DE GESTAO ... resolve: Dispensar ..." scored +2 for
# "diretor" when the actual post being vacated was a Chefe de Divisao FC-3.
# 36 of 65 position hits in the first two months were this false positive.
# So: trust an explicit CCE/FCE/DAS code anywhere, but accept a role word only
# when it is grammatically attached to a cargo/funcao being conferred or removed.
ROLE_WORDS = r'coordenador[a]?-geral|diretor[a]?|assessor[a]? especial|secretári[oa]'
CARGO_CONTEXT = re.compile(
    r'(?:cargo|função|funcao|cargo comissionado executivo|'
    r'função comissionada executiva|funcao comissionada executiva)'
    r'\s+(?:comissionado\s+)?(?:executivo\s+)?(?:de\s+|da\s+|do\s+)?'
    r'(' + ROLE_WORDS + r')',
    re.IGNORECASE)
# "exercer o cargo de X" / "para exercer a função de X"
EXERCER_CONTEXT = re.compile(
    r'exercer\s+(?:o|a)\s+(?:cargo|função|funcao)[^,\.]{0,40}?(' + ROLE_WORDS + r')',
    re.IGNORECASE)


def extract_target_position(full_text):
    """Return the position level of the cargo actually at stake, or ''.

    Prefers an explicit CCE/FCE/DAS code; falls back to a role word only when it
    sits in cargo/função context. Returns '' for the signing-authority case.
    """
    # Explicit code — unambiguous, and already filtered for level by is_noise().
    m = re.search(r'\b((?:CCE|FCE|DAS)\s*\d+\.\d+)\b', full_text, re.IGNORECASE)
    if m:
        code = re.sub(r'\s+', ' ', m.group(1).upper()).strip()
        if any(code == kw.upper() for kw in POSITION_KEYWORDS):
            return code
        return ''  # a code we know about, but below coordenador-geral
    for pat in (CARGO_CONTEXT, EXERCER_CONTEXT):
        m = pat.search(full_text)
        if m:
            return m.group(1).lower()
    return ''


def classify_result(item):
    """Classify a DOU result and compute relevance score.

    Score breakdown (shown in digest):
      +3  target organ (Brasília executive branch)
      +3  real vacancy (exoneração/dispensa, not substituto, not filled)
      +2  opportunity (processo seletivo, edital)
      +2  position level >= coordenador-geral
      +1  cessão / movement intel
      expertise keywords: +3/tier1, +2/tier2, +1/tier3
    """
    title = clean_html(item.get('title', '')).lower()
    content = clean_html(item.get('content', '')).lower()
    hierarchy = [h.lower() for h in item.get('hierarchyList', [])]
    full_text = f'{title} {content}'

    classification = {
        'type': '',
        'relevance': 0,
        'position_level': '',
        'target_organ': False,
        'expertise_match': [],
    }

    if is_noise(full_text):
        classification['type'] = 'skip'
        return classification

    # +3 target organ — hierarchy ONLY.
    # Matching against full_text made almost anything a "target": a portaria that
    # merely cites "Ministério da Fazenda" in its legal preamble scored the +3.
    for organ in TARGET_ORGANS:
        if any(organ in h for h in hierarchy):
            classification['target_organ'] = True
            classification['relevance'] += 3
            break

    # Detect type
    vacancy_words = ['exonerar', 'exoneração', 'dispensar', 'dispensa',
                     'vacância', 'cargo vago']
    opportunity_words = ['processo seletivo', 'edital de seleção',
                         'seleção simplificada', 'chamada pública']
    cessao_words = ['ceder', 'cessão', 'disponibilizar a requisição']

    if any(w in full_text for w in vacancy_words):
        classification['type'] = 'VAC'
        classification['relevance'] += 3
    elif any(w in full_text for w in opportunity_words):
        classification['type'] = 'OPP'
        classification['relevance'] += 2
    elif any(w in full_text for w in cessao_words):
        classification['type'] = 'MOV'
        classification['relevance'] += 1
    else:
        classification['type'] = 'skip'
        return classification

    # +2 position level — must be the cargo in question, not the signer.
    pos = extract_target_position(full_text)
    if pos:
        classification['position_level'] = pos
        classification['relevance'] += 2

    # Tiered expertise keywords
    exp_score, exp_matches = score_expertise(full_text)
    classification['expertise_match'] = exp_matches
    classification['relevance'] += exp_score

    return classification


def fetch_article_text(url_title):
    """Fetch full text of a DOU article."""
    url = f'https://www.in.gov.br/web/dou/-/{url_title}'
    try:
        from bs4 import BeautifulSoup
        r = requests.get(url, headers=HEADERS, timeout=30)
        soup = BeautifulSoup(r.text, 'html.parser')
        materia = soup.find(id='materia')
        if materia:
            return materia.get_text(separator='\n', strip=True)
    except Exception:
        pass
    return ''


def run_queries(queries, period='dia', filter_noise=True):
    """Run a list of queries and return all results with classifications."""
    all_results = []
    seen_urls = set()

    for q in queries:
        items = search_dou(q['query'], q['secao'], period)
        print(f"  {q['label']}: {len(items)} results")
        for item in items:
            url_title = item.get('urlTitle', '')
            if url_title in seen_urls:
                continue
            if filter_noise and is_excluded_organ(item):
                continue
            seen_urls.add(url_title)

            classification = classify_result(item)
            if filter_noise and classification['type'] == 'skip':
                continue
            result = {
                'date': item.get('pubDate', ''),
                'title': clean_html(item.get('title', '')),
                'organ': ' > '.join(item.get('hierarchyList', [])),
                'url': f'https://www.in.gov.br/web/dou/-/{url_title}',
                'content_preview': clean_html(item.get('content', ''))[:500],
                'query_label': q['label'],
                'type': classification['type'],
                'relevance': classification['relevance'],
                'position_level': classification['position_level'],
                'target_organ': classification['target_organ'],
                'expertise_match': '|'.join(classification['expertise_match']),
            }
            all_results.append(result)

    return all_results


def save_results(results, filename):
    """Append new results to CSV, avoiding duplicates."""
    filepath = DATA_DIR / filename
    if filepath.exists():
        existing = pd.read_csv(filepath, sep=';')
        existing_urls = set(existing['url'].values)
    else:
        existing = pd.DataFrame()
        existing_urls = set()

    new_results = [r for r in results if r['url'] not in existing_urls]
    if not new_results:
        return 0

    new_df = pd.DataFrame(new_results)
    combined = pd.concat([existing, new_df], ignore_index=True)
    combined.to_csv(filepath, sep=';', index=False)
    return len(new_results)


def extract_person_name(text):
    """Try to extract a person's name from portaria text."""
    patterns = [
        r'servidor[a]?\s+([A-ZÁÉÍÓÚÀÂÊÔÃÕÇ][A-ZÁÉÍÓÚÀÂÊÔÃÕÇ\s]+?)(?:,|\s*matrícula|\s*SIAPE|\s*ocupante)',
        r'servidora\s+([A-ZÁÉÍÓÚÀÂÊÔÃÕÇ][A-ZÁÉÍÓÚÀÂÊÔÃÕÇ\s]+?)(?:,|\s*matrícula|\s*SIAPE|\s*ocupante)',
        r'(?:EXONERAR|NOMEAR|DESIGNAR|DISPENSAR|CEDER)\s+(?:o servidor |a servidora |)([A-ZÁÉÍÓÚÀÂÊÔÃÕÇ][A-ZÁÉÍÓÚÀÂÊÔÃÕÇ\s]+?)(?:,|\s*matrícula|\s*SIAPE)',
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            name = m.group(1).strip()
            if len(name) > 5 and len(name) < 60:
                return name.title()
    return ''


def extract_action(text):
    """Extract what happened: cessão, exoneração, designação, etc."""
    text_lower = text.lower()
    actions = [
        ('ceder', 'cessão'),
        ('disponibilizar a requisição', 'requisição'),
        ('exonerar', 'exoneração'),
        ('dispensar', 'dispensa'),
        ('designar', 'designação'),
        ('nomear', 'nomeação'),
        ('autorizar o afastamento', 'afastamento'),
        ('afastamento do país', 'afastamento'),
        ('aposentadoria', 'aposentadoria'),
        ('vacância', 'vacância'),
        ('substituir', 'substituição'),
        ('substitut', 'substituição'),
    ]
    for keyword, label in actions:
        if keyword in text_lower:
            return label
    return ''


def extract_position(text):
    """Extract the position/cargo code (CCE/FCE/DAS) mentioned."""
    code_pat = r'((?:CCE|FCE|DAS)\s*\d+\.\d+)'
    m = re.search(code_pat, text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    role_pat = r'(Coordenador-Geral|Diretor[a]?|Assessor[a]? Especial|Secretári[oa])'
    m = re.search(role_pat, text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return ''


def extract_destination(text):
    """Extract where the person is going (for cessões)."""
    patterns = [
        r'para exercer[^,]*?(?:no|na|do|da)\s+(.+?)(?:\.|$)',
        r'(?:do|da|no|na)\s+(Ministério[^,\.]+)',
        r'(?:do|da|no|na)\s+(Secretaria[^,\.]+)',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            dest = m.group(1).strip()[:100]
            if len(dest) > 10:
                return dest
    return ''


def summarize_ipea_item(result):
    """One-line summary of an IPEA personnel change."""
    text = result['content_preview']
    name = extract_person_name(text)
    action = extract_action(text)
    position = extract_position(text)

    if name and action:
        line = f'**{name}** — {action}'
        if position:
            line += f', {position}'
        return line
    elif name:
        return f'**{name}**'
    else:
        return result['title']


def format_organ_short(organ_str):
    """Shorten organ hierarchy to last meaningful level."""
    parts = [p.strip() for p in organ_str.split('>')]
    if len(parts) >= 2:
        return parts[-1].strip()
    return organ_str


def generate_digest(ipea_results, radar_results):
    """Generate a clean, scannable markdown digest."""
    today = datetime.date.today().strftime('%d/%m/%Y')

    lines = [
        f'## DOU Career Radar — {today}',
        '',
    ]

    # --- IPEA section: one-liners ---
    if ipea_results:
        lines.append('### IPEA')
        lines.append('')
        for r in ipea_results:
            summary = summarize_ipea_item(r)
            lines.append(f'- {summary} [>>]({r["url"]})')
        lines.append('')

    # --- Radar section: only items scoring >= 5 (organ + signal minimum) ---
    relevant = [r for r in radar_results if r['relevance'] >= 5]
    if relevant:
        lines.append('### Radar')
        lines.append('')
        lines.append('| Sc | Type | Organ | Position | Keywords | |')
        lines.append('|---:|------|-------|----------|----------|----|')
        for r in sorted(relevant, key=lambda x: -x['relevance']):
            score = r['relevance']
            rtype = r['type']
            organ = format_organ_short(r['organ'])
            pos = r['position_level'] or ''
            kw = r['expertise_match'].replace('|', ', ') if r['expertise_match'] else ''
            link = f'[>>]({r["url"]})'
            lines.append(f'| {score} | {rtype} | {organ} | {pos} | {kw} | {link} |')
        lines.append('')

    # --- Footer ---
    n_vac = sum(1 for r in radar_results if r['type'] == 'VAC')
    n_opp = sum(1 for r in radar_results if r['type'] == 'OPP')
    n_mov = sum(1 for r in radar_results if r['type'] == 'MOV')
    lines.append(f'*VAC: {n_vac} | OPP: {n_opp} | MOV: {n_mov} | '
                 f'IPEA: {len(ipea_results)}*')
    lines.append('')
    lines.append('*Score: +3 organ/vacancy, +2 opportunity/position, '
                 '+1 movement. Keywords: +3 core, +2 domain, +1 context*')

    return '\n'.join(lines)


def _rel(row):
    """Relevance as an int, whether the row came from a scan or from CSV."""
    try:
        return int(float(row.get('relevance', 0) or 0))
    except (TypeError, ValueError):
        return 0


def _read_csv_window(filename, days=7):
    """Return rows from a data CSV whose pubDate falls in the last `days` days."""
    filepath = DATA_DIR / filename
    if not filepath.exists():
        return []
    cutoff = datetime.date.today() - datetime.timedelta(days=days)
    rows = []
    with open(filepath, encoding='utf-8', newline='') as fh:
        for row in csv.DictReader(fh, delimiter=';'):
            try:
                dt = datetime.datetime.strptime(row.get('date', ''), '%d/%m/%Y').date()
            except (ValueError, TypeError):
                continue
            if dt >= cutoff:
                rows.append({k: (v if v is not None else '') for k, v in row.items()})
    return rows


def generate_weekly_digest(days=7):
    """Build the Friday digest from the accumulated CSVs, not just today's scan.

    The daily README is a running log; this is the thing actually worth emailing.
    Threshold is deliberately higher than the daily view (>=7, not >=5): two months
    of data showed ~3 items/day at >=5, of which roughly one per three weeks
    mattered. A weekly note with a handful of real leads beats a daily list nobody
    opens.
    """
    today = datetime.date.today()
    start = today - datetime.timedelta(days=days)
    radar = _read_csv_window('dou_radar.csv', days)
    ipea = _read_csv_window('dou_ipea.csv', days)

    lines = [
        f'# DOU Career Radar — semana de {start.strftime("%d/%m")} a {today.strftime("%d/%m/%Y")}',
        '',
    ]

    def _fmt(rows, show_kw=True):
        out = ['| Sc | Data | Órgão | Cargo | Palavras-chave | |',
               '|---:|------|-------|-------|----------------|----|']
        for r in rows:
            kw = str(r.get('expertise_match', '') or '').replace('|', ', ')
            out.append(
                f'| {_rel(r)} | {r["date"]} '
                f'| {format_organ_short(str(r["organ"]))} '
                f'| {r.get("position_level", "") or ""} '
                f'| {kw if show_kw else ""} '
                f'| [>>]({r["url"]}) |'
            )
        return out

    # --- Opportunities first: these are the actionable ones ---
    opps = sorted([r for r in radar if r.get('type') == 'OPP'],
                  key=lambda x: -_rel(x))
    if opps:
        lines += ['## Oportunidades', '', *_fmt(opps), '']

    # --- Vacancies worth a look ---
    vacs = sorted([r for r in radar
                   if r.get('type') in ('VAC', 'MOV') and _rel(r) >= 7],
                  key=lambda x: -_rel(x))
    if vacs:
        lines += [f'## Vagas abertas (score >= 7) — {len(vacs)} de '
                  f'{sum(1 for r in radar if r.get("type") in ("VAC", "MOV"))} na semana',
                  '', *_fmt(vacs), '']

    # --- IPEA personnel ---
    if ipea:
        lines += ['## IPEA', '']
        for r in ipea:
            lines.append(f'- {summarize_ipea_item(r)} [>>]({r["url"]})')
        lines.append('')

    if not (opps or vacs or ipea):
        lines += ['*Nada relevante esta semana.*', '']

    lines += [
        '---',
        '**Checagem manual:** '
        '[SIGEPE Oportunidades](https://oportunidades.sigepe.gov.br) '
        '— filtrar por coordenador-geral / diretor / CCE >= 1.13.',
        '',
        f'*Semana: {len(radar)} sinais no radar '
        f'({sum(1 for r in radar if r.get("type") == "OPP")} OPP, '
        f'{sum(1 for r in radar if r.get("type") == "VAC")} VAC), '
        f'{len(ipea)} IPEA.*',
    ]
    return '\n'.join(lines)


def scrape_enap_vagas():
    """Scrape ENAP /vagas/ for open selection processes only."""
    url = 'https://enap.gov.br/vagas/'
    try:
        from bs4 import BeautifulSoup
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')
    except Exception as e:
        print(f'  ENAP fetch failed: {e}')
        return []

    results = []
    today = datetime.date.today().strftime('%d/%m/%Y')

    in_open_section = False
    for tag in soup.find_all(['h2', 'h3']):
        if tag.name == 'h2':
            text = tag.get_text(strip=True).lower()
            if 'andamento' in text:
                in_open_section = True
            elif 'encerrad' in text:
                break
            continue

        if not in_open_section or tag.name != 'h3':
            continue

        title = tag.get_text(strip=True)
        if not title:
            continue

        link_tag = tag.find_parent().find('a', href=True) if tag.find_parent() else None
        if not link_tag:
            link_tag = tag.find_next('a', href=True)
        href = link_tag['href'] if link_tag else ''
        if not href:
            continue
        if not href.startswith('http'):
            href = f'https://enap.gov.br{href}'

        classification = classify_result({
            'title': title, 'content': title,
            'hierarchyList': ['ENAP'],
        })
        if classification['type'] == 'skip':
            classification['type'] = 'OPP'
            classification['relevance'] = 2

        results.append({
            'date': today,
            'title': title,
            'organ': 'ENAP',
            'url': href,
            'content_preview': title,
            'query_label': 'ENAP Vagas',
            'type': classification['type'],
            'relevance': classification['relevance'],
            'position_level': classification['position_level'],
            'target_organ': True,
            'expertise_match': '|'.join(classification.get('expertise_match', [])),
        })

    return results


def main():
    DATA_DIR.mkdir(exist_ok=True)
    today = datetime.date.today().strftime('%Y-%m-%d')
    period = 'dia'

    print(f'=== DOU Career Radar — {today} ===\n')

    # 1. Original IPEA tracking — no noise filter, fetch full text
    print('Searching IPEA personnel...')
    ipea_results = run_queries(IPEA_QUERIES, period, filter_noise=False)
    for r in ipea_results:
        url_title = r['url'].split('/-/')[-1] if '/-/' in r['url'] else ''
        if url_title:
            full = fetch_article_text(url_title)
            if full:
                r['content_preview'] = full[:2000]

    # 2. Vacancy signals (DOU Section 2)
    print('\nSearching vacancy signals...')
    vacancy_results = run_queries(VACANCY_QUERIES, period)

    # 3. Opportunity signals (DOU Sections 1, 2 and 3)
    print('\nSearching opportunities...')
    opportunity_results = run_queries(OPPORTUNITY_QUERIES, period)

    # 4. ENAP open selections
    print('\nScraping ENAP /vagas/...')
    enap_results = scrape_enap_vagas()
    print(f'  ENAP: {len(enap_results)} listings found')

    # Combine radar results
    radar_results = vacancy_results + opportunity_results + enap_results

    # Save to CSV files
    ipea_new = save_results(ipea_results, 'dou_ipea.csv')
    radar_new = save_results(radar_results, 'dou_radar.csv')
    print(f'\nNew IPEA entries: {ipea_new}')
    print(f'New radar entries: {radar_new}')

    # Daily digest — a running log committed to the repo, not emailed.
    has_news = ipea_new > 0 or radar_new > 0
    if has_news or not (DATA_DIR / 'dou_ipea.csv').exists():
        digest = generate_digest(ipea_results, radar_results)
        with open('README.md', 'w') as f:
            f.write(digest)
        print('\nDigest written to README.md')
    else:
        print('\nNo new results today.')

    # Weekly digest — this is what gets emailed (Fridays, or on demand).
    if datetime.date.today().weekday() == 4 or os.environ.get('FORCE_WEEKLY'):
        weekly = generate_weekly_digest()
        with open('WEEKLY.md', 'w') as f:
            f.write(weekly)
        print('Weekly digest written to WEEKLY.md')

    return has_news


if __name__ == '__main__':
    main()
