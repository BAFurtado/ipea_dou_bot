#!/usr/bin/env python3
"""
SUPERSEDED by sigepe_editais.py — kept only as the record of a dead end.

This module needs a gov.br bearer token AND an atribuição on the Oportunidades
module that this account does not have (the login works; the page refuses it).
sigepe_editais.py reaches the same editais through the portal's public,
unauthenticated HTML listing, needs no token, and runs in CI. Use that.

SIGEPE Oportunidades checker — runs LOCALLY, not in GitHub Actions.

Why local: oportunidades.sigepe.gov.br is an Angular SPA backed by
  https://prod-sigepe-api.estaleiro.serpro.gov.br/oportunidades
Every endpoint (/editais, /oportunidades, /orgaos, /situacoes-edital, and the
banco-talentos servidor API) returns 401 Unauthorized without a gov.br SSO
bearer token — verified 2026-08-20, including guesses at "public" paths. There is
no anonymous listing to poll, so this cannot live in CI on a schedule.

  HOW TO GET A TOKEN
  1. Log in at https://oportunidades.sigepe.gov.br (gov.br / Login Único).
  2. DevTools -> Network -> click any request to prod-sigepe-api...
  3. Copy the full value of the `Authorization` request header.
  4. Either:
       export SIGEPE_TOKEN='Bearer eyJ...'
     or write that same line to ~/.config/sigepe/token  (chmod 600)

  Tokens are short-lived (typically under an hour). A 401 means: log in again
  and re-copy. That is expected, not a bug.

  FIRST RUN: use --dump. The response schema below is INFERRED from the
  compiled frontend bundle, not from a live response — it has never been
  validated against real data. --dump writes the raw JSON so you can confirm the
  field names and adjust FIELD_MAP if they differ.

Usage:
    python sigepe_check.py                # score and print open opportunities
    python sigepe_check.py --dump         # also save raw JSON for schema checking
    python sigepe_check.py --all          # do not filter by score
    python sigepe_check.py --out SIGEPE.md
"""
import os
import sys
import json
import argparse
import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))  # moved to old/; career_radar lives one level up
from career_radar import (  # noqa: E402
    score_expertise, TARGET_ORGANS, POSITION_KEYWORDS, LOW_POSITIONS,
)

API = 'https://prod-sigepe-api.estaleiro.serpro.gov.br/oportunidades'
DATA_DIR = Path(__file__).parent / 'data'
TOKEN_FILE = Path.home() / '.config' / 'sigepe' / 'token'

HEADERS_BASE = {
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'pt-BR,pt;q=0.9',
    'Origin': 'https://oportunidades.sigepe.gov.br',
    'Referer': 'https://oportunidades.sigepe.gov.br/',
    'User-Agent': ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'),
}

# Inferred from the frontend bundle's model classes. Each entry lists the field
# names to try, in order — the first one present wins. Adjust after --dump.
FIELD_MAP = {
    'id':        ('id', 'idOportunidade', 'oportunidadeId'),
    'title':     ('nome', 'nomeOportunidade', 'titulo', 'descricao'),
    'organ':     ('orgao', 'nomeOrgao', 'siglaOrgao', 'unidadeOrganizacional'),
    'uorg':      ('uorg', 'nomeUorg', 'unidade', 'lotacao'),
    'vacancies': ('vagas', 'quantidadeVagas', 'numeroVagas'),
    'movement':  ('tipoMovimentacao', 'movimentacao', 'tipo'),
    'locality':  ('localidade', 'cidade', 'municipio'),
    'deadline':  ('dataFinal', 'data_final', 'dataFimInscricao', 'dataEncerramento'),
    'start':     ('dataInicial', 'data_inicial', 'dataInicioInscricao'),
    'status':    ('situacao', 'situacaoOportunidade', 'status'),
    'edital':    ('numeroEdital', 'numero_edital', 'edital'),
    'about':     ('sobreVagas', 'descricaoVagas', 'atribuicoes', 'detalhamento'),
}


def get_token():
    tok = os.environ.get('SIGEPE_TOKEN', '').strip()
    if not tok and TOKEN_FILE.exists():
        tok = TOKEN_FILE.read_text(encoding='utf-8').strip()
    if not tok:
        sys.exit(
            'No token found.\n'
            f'  Set SIGEPE_TOKEN, or write the Authorization header to {TOKEN_FILE}\n'
            '  See the module docstring for how to obtain it.'
        )
    return tok if tok.lower().startswith('bearer ') else f'Bearer {tok}'


def api_get(path, token, **params):
    """GET an endpoint. Returns parsed JSON, or None on failure."""
    url = f'{API}/{path.lstrip("/")}'
    headers = dict(HEADERS_BASE, Authorization=token)
    try:
        r = requests.get(url, headers=headers, params=params or None, timeout=40)
    except requests.RequestException as e:
        print(f'  ! {path}: request failed — {e}', file=sys.stderr)
        return None
    if r.status_code == 401:
        print(f'  ! {path}: 401 Unauthorized — token expired, log in and re-copy it.',
              file=sys.stderr)
        return None
    if r.status_code == 403:
        print(f'  ! {path}: 403 Forbidden — your gov.br profile may lack access '
              'to this module.', file=sys.stderr)
        return None
    if not r.ok:
        print(f'  ! {path}: HTTP {r.status_code}', file=sys.stderr)
        return None
    try:
        return r.json()
    except ValueError:
        print(f'  ! {path}: response was not JSON', file=sys.stderr)
        return None


def unwrap(payload):
    """SIGEPE wraps lists inconsistently — find the list of records."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ('content', 'items', 'dados', 'resultado', 'lista',
                    'oportunidades', 'editais', '_embedded'):
            val = payload.get(key)
            if isinstance(val, list):
                return val
            if isinstance(val, dict):
                inner = unwrap(val)
                if inner:
                    return inner
        # a single record
        if any(k in payload for k in ('id', 'nome', 'situacao')):
            return [payload]
    return []


def pick(record, key):
    """Pull a logical field out of a record, tolerating schema drift."""
    for cand in FIELD_MAP.get(key, ()):
        if cand in record and record[cand] not in (None, '', []):
            val = record[cand]
            if isinstance(val, dict):
                for sub in ('nome', 'descricao', 'sigla', 'valor', 'id'):
                    if sub in val:
                        return str(val[sub])
                return json.dumps(val, ensure_ascii=False)[:120]
            if isinstance(val, list):
                return ', '.join(str(v) for v in val[:3])
            return str(val)
    return ''


def score(record):
    """Score one opportunity on the same basis the DOU radar uses."""
    parts = [pick(record, k) for k in
             ('title', 'organ', 'uorg', 'about', 'movement')]
    text = ' '.join(parts).lower()

    relevance = 0
    reasons = []

    if any(o in text for o in TARGET_ORGANS):
        relevance += 3
        reasons.append('órgão-alvo(+3)')

    pos = ''
    if any(kw.lower() in text for kw in POSITION_KEYWORDS):
        pos = next(kw for kw in POSITION_KEYWORDS if kw.lower() in text)
        relevance += 2
        reasons.append(f'{pos}(+2)')
    elif any(kw.lower() in text for kw in LOW_POSITIONS):
        relevance -= 2
        reasons.append('cargo baixo(-2)')

    exp_score, exp_matches = score_expertise(text)
    relevance += exp_score
    reasons += exp_matches

    return relevance, pos, reasons


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dump', action='store_true',
                    help='save raw JSON to data/sigepe_raw.json for schema checking')
    ap.add_argument('--all', action='store_true', help='do not filter by score')
    ap.add_argument('--min-score', type=int, default=3)
    ap.add_argument('--size', type=int, default=100, help='page size')
    ap.add_argument('--out', default='SIGEPE.md')
    args = ap.parse_args()

    token = get_token()
    DATA_DIR.mkdir(exist_ok=True)

    raw = {}
    records = []
    for path in ('oportunidades', 'editais'):
        payload = api_get(path, token, page=0, size=args.size)
        raw[path] = payload
        found = unwrap(payload)
        print(f'  {path}: {len(found)} records')
        for rec in found:
            if isinstance(rec, dict):
                rec['_source'] = path
                records.append(rec)

    if args.dump:
        dump_path = DATA_DIR / 'sigepe_raw.json'
        dump_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2),
                             encoding='utf-8')
        print(f'  raw JSON -> {dump_path}')

    if not records:
        print('\nNo records returned. If you saw a 401 above, the token expired.\n'
              'If the calls succeeded but returned nothing, run with --dump and '
              'check the field names against FIELD_MAP.')
        return 1

    scored = []
    for rec in records:
        rel, pos, reasons = score(rec)
        scored.append((rel, pos, reasons, rec))
    scored.sort(key=lambda x: -x[0])

    shown = scored if args.all else [s for s in scored if s[0] >= args.min_score]

    today = datetime.date.today().strftime('%d/%m/%Y')
    lines = [f'# SIGEPE Oportunidades — {today}', '',
             f'{len(records)} vagas retornadas, {len(shown)} acima do corte '
             f'(score >= {args.min_score}).', '',
             '| Sc | Órgão | Vaga | Cargo | Movimentação | Prazo | Match |',
             '|---:|-------|------|-------|--------------|-------|-------|']
    for rel, pos, reasons, rec in shown:
        lines.append(
            f'| {rel} | {pick(rec, "organ") or pick(rec, "uorg")} '
            f'| {pick(rec, "title")} | {pos} | {pick(rec, "movement")} '
            f'| {pick(rec, "deadline")} | {", ".join(reasons)} |'
        )
    out = '\n'.join(lines)

    Path(args.out).write_text(out + '\n', encoding='utf-8')
    print()
    print(out)
    print(f'\nWritten to {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
