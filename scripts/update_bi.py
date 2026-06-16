#!/usr/bin/env python3
"""
Atualiza o BI de reuniões Smiller com dados do Acuity Scheduling.
Roda via GitHub Actions — não requer browser.
"""

import os
import re
import json
import datetime
import sys
import requests

# --- Credenciais via environment variables (GitHub Secrets) ---
ACUITY_USER_ID = os.environ["ACUITY_USER_ID"]
ACUITY_API_KEY = os.environ["ACUITY_API_KEY"]
GUSTAVO_CAL    = "14153461"
LAURA_CAL      = "13908391"
ACUITY_BASE    = "https://acuityscheduling.com/api/v1"


# ---------------------------------------------------------------------------
# Helpers de data
# ---------------------------------------------------------------------------

def semana(offset: int) -> dict:
    """Retorna min/max/label/mes para uma semana relativa a hoje."""
    hoje = datetime.date.today()
    seg  = hoje - datetime.timedelta(days=hoje.weekday()) + datetime.timedelta(weeks=offset)
    sex  = seg + datetime.timedelta(days=4)
    lbl  = lambda d: d.strftime("%d/%m")
    return {
        "min":   seg.isoformat(),
        "max":   sex.isoformat(),
        "label": f"{lbl(seg)}-{lbl(sex)}",
        "mes":   seg.strftime("%Y-%m"),
    }


# ---------------------------------------------------------------------------
# Acuity API
# ---------------------------------------------------------------------------

def acuity_get(path: str, params: dict = None) -> list | dict:
    r = requests.get(
        f"{ACUITY_BASE}{path}",
        params=params,
        auth=(ACUITY_USER_ID, ACUITY_API_KEY),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def week_counts(cal_id: str, semanas: list[dict]) -> list[dict]:
    """Conta realizadas e canceladas por semana."""
    weeks = []
    for s in semanas:
        base_params = {"calendarID": cal_id, "minDate": s["min"], "maxDate": s["max"], "max": 200}
        realized = len(acuity_get("/appointments", base_params))
        canceled = len(acuity_get("/appointments", {**base_params, "canceled": "true"}))
        weeks.append({"label": s["label"], "mes": s["mes"], "realized": realized, "canceled": canceled, "noshow": 0})
        print(f"  {s['label']}: {realized}r / {canceled}c")
    return weeks


def parse_appt(appt: dict, prof: str) -> dict:
    cx      = next((f for f in (appt.get("forms") or []) if f["name"] == "Forms CX"), None)
    payload = ""
    paciente = ""
    poronde  = "-"

    if cx:
        for v in cx.get("values", []):
            if v["name"] == "Payload":
                payload  = v.get("value", "")
            elif v["name"] == "Paciente":
                paciente = v.get("value", "")
            elif v["name"] == "PorOnde":
                poronde  = v.get("value", "") or "-"

    def get(label: str) -> str:
        m = re.search(label + r":\s*([^\n]+)", payload)
        return m.group(1).strip() if m else ""

    # Parse local datetime (Acuity retorna com offset, ex: -0300)
    dt_str = appt["datetime"]  # "2026-06-17T14:00:00-0300"
    # Python 3.7+ fromisoformat não aceita -0300, precisa de ":"
    dt_str_fixed = re.sub(r"([+-])(\d{2})(\d{2})$", r"\1\2:\3", dt_str)
    dt = datetime.datetime.fromisoformat(dt_str_fixed)
    data_str = dt.strftime("%Y-%m-%d")
    dias = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sab", "Dom"]
    dia_str = f"{dias[dt.weekday()]} {dt.day:02d}/{dt.month:02d}"

    return {
        "data":        data_str,
        "mes":         data_str[:7],
        "prof":        prof,
        "tipo":        appt.get("type", ""),
        "dia":         dia_str,
        "cliente":     f"{appt.get('firstName', '')} {appt.get('lastName', '')}",
        "status":      get("Status do tratamento"),
        "mecanica":    get("Mecanica auxiliar") or get(r"Mec[aâ]nica auxiliar"),
        "suporte":     get("Suporte anterior da equipe"),
        "complexidade": get("Complexidade do caso"),
        "agenda":      get("Agenda"),
        "paciente":    paciente,
        "poronde":     poronde,
    }


def week_appts(cal_id: str, prof: str, semana_atual: dict) -> list[dict]:
    appts = acuity_get("/appointments", {
        "calendarID": cal_id,
        "minDate":    semana_atual["min"],
        "maxDate":    semana_atual["max"],
        "max":        200,
    })
    return [parse_appt(a, prof) for a in appts]


# ---------------------------------------------------------------------------
# Parsear bloco DATA do HTML (JS object literal → Python dict)
# ---------------------------------------------------------------------------

def extract_data_block(html: str) -> tuple[dict, int, int]:
    """Extrai o bloco `const DATA = {...}` e retorna (dict, start_pos, end_pos)."""
    marker = "const DATA ="
    start = html.find(marker)
    if start == -1:
        raise ValueError("Bloco 'const DATA =' não encontrado no HTML")

    # Encontrar abertura do objeto
    i = start + len(marker)
    while i < len(html) and html[i] != "{":
        i += 1

    # Contar profundidade de chaves para achar o fim
    depth, end = 0, -1
    for j in range(i, len(html)):
        if html[j] == "{":
            depth += 1
        elif html[j] == "}":
            depth -= 1
            if depth == 0:
                end = j
                break

    if end == -1:
        raise ValueError("Não conseguiu encontrar o fim do bloco DATA")

    js_block = html[i : end + 1]

    # Converter JS object literal → JSON válido
    # Estratégia: proteger strings antes de aplicar regex nas chaves,
    # para evitar que valores como "T17:02:25" sejam modificados.

    # 1. Substituir todas as strings por placeholders
    protected = []
    def protect(m):
        protected.append(m.group(0))
        return f'"__S{len(protected) - 1}__"'
    js_safe = re.sub(r'"(?:[^"\\]|\\.)*"', protect, js_block)

    # 2. Adicionar aspas em chaves não-quotadas (agora seguro)
    js_safe = re.sub(r'\b(\w+)\b(\s*):', r'"\1"\2:', js_safe)

    # 3. Remover vírgulas finais antes de } ou ]
    js_safe = re.sub(r",(\s*[}\]])", r"\1", js_safe)

    # 4. Restaurar strings originais
    for idx, original in enumerate(protected):
        js_safe = js_safe.replace(f'"__S{idx}__"', original)

    try:
        data = json.loads(js_safe)
    except json.JSONDecodeError as e:
        snippet = js_safe[max(0, e.pos - 80) : e.pos + 80]
        raise ValueError(f"Erro ao parsear JSON: {e}\nTrecho: ...{snippet}...") from e

    return data, start + len(marker), end


# ---------------------------------------------------------------------------
# Merge de semanas
# ---------------------------------------------------------------------------

def norm_label(label: str) -> str:
    return re.sub(r"[–—\-\s]", "", label).lower()


def merge_weeks(existing: list[dict], new: list[dict]) -> list[dict]:
    week_map = {norm_label(w["label"]): dict(w) for w in existing}
    for w in new:
        key = norm_label(w["label"])
        if key in week_map:
            week_map[key]["realized"] = w["realized"]
            if w["canceled"] > 0:
                week_map[key]["canceled"] = w["canceled"]
        else:
            week_map[key] = dict(w)
    return sorted(week_map.values(), key=lambda w: w["mes"] + w["label"])


# ---------------------------------------------------------------------------
# Geração do novo bloco DATA
# ---------------------------------------------------------------------------

def esc(v) -> str:
    return str(v or "").replace("\\", "\\\\").replace('"', '\\"')


def build_data_block(merged: dict) -> str:
    def weeks_js(weeks):
        lines = [
            f'        {{ label:"{w["label"]}", mes:"{w["mes"]}", realized:{w["realized"]}, canceled:{w["canceled"]}, noshow:{w["noshow"]} }}'
            for w in weeks
        ]
        return ",\n".join(lines)

    def appts_js(appts):
        lines = [
            f'      {{data:"{esc(a["data"])}",mes:"{esc(a["mes"])}",prof:"{esc(a["prof"])}",tipo:"{esc(a["tipo"])}",dia:"{esc(a["dia"])}",cliente:"{esc(a["cliente"])}",status:"{esc(a["status"])}",mecanica:"{esc(a["mecanica"])}",suporte:"{esc(a["suporte"])}",complexidade:"{esc(a["complexidade"])}",agenda:"{esc(a["agenda"])}",paciente:"{esc(a["paciente"])}",poronde:"{esc(a["poronde"])}"}}'
            for a in appts
        ]
        return ",\n".join(lines)

    g = merged["people"]["gustavo"]
    la = merged["people"]["laura"]
    now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")

    return f"""{{
  generatedAt: "{now}",
  people: {{
    gustavo: {{
      name: "{g['name']}", initials: "{g['initials']}", cls: "{g['cls']}",
      weeks: [
{weeks_js(g['weeks'])}
      ]
    }},
    laura: {{
      name: "{la['name']}", initials: "{la['initials']}", cls: "{la['cls']}",
      weeks: [
{weeks_js(la['weeks'])}
      ]
    }}
  }},
  appointments: [
{appts_js(merged['appointments'])}
  ]
}}"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("=== BI Smiller — Atualização automática ===")

    semanas       = [semana(i) for i in range(-4, 1)]
    semana_atual  = semanas[-1]
    print(f"Semana atual: {semana_atual['min']} → {semana_atual['max']}")

    print("\n📅 Gustavo Bernardes:")
    g_weeks = week_counts(GUSTAVO_CAL, semanas)

    print("\n📅 Laura Raposo:")
    l_weeks = week_counts(LAURA_CAL, semanas)

    print("\n🔍 Appointments detalhados (semana atual)...")
    g_appts = week_appts(GUSTAVO_CAL, "Gustavo", semana_atual)
    l_appts = week_appts(LAURA_CAL,   "Laura",   semana_atual)
    print(f"  Gustavo: {len(g_appts)} | Laura: {len(l_appts)}")

    print("\n📄 Lendo index.html...")
    html_path = "index.html"
    with open(html_path, encoding="utf-8") as f:
        html = f.read()

    existing_data, data_start, data_end = extract_data_block(html)

    # Merge appointments
    new_appts = sorted(g_appts + l_appts, key=lambda a: a["data"])
    if new_appts:
        min_date = min(a["data"] for a in new_appts)
        max_date = max(a["data"] for a in new_appts)
        kept = [a for a in (existing_data.get("appointments") or [])
                if a["data"] < min_date or a["data"] > max_date]
    else:
        kept = existing_data.get("appointments") or []
    all_appts = sorted(kept + new_appts, key=lambda a: a["data"])

    merged = {
        "people": {
            "gustavo": {
                **existing_data["people"]["gustavo"],
                "weeks": merge_weeks(existing_data["people"]["gustavo"]["weeks"], g_weeks),
            },
            "laura": {
                **existing_data["people"]["laura"],
                "weeks": merge_weeks(existing_data["people"]["laura"]["weeks"], l_weeks),
            },
        },
        "appointments": all_appts,
    }

    new_block = build_data_block(merged)
    new_html  = html[: data_start] + " " + new_block + html[data_end + 1 :]

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(new_html)

    print(f"\n✅ index.html atualizado:")
    print(f"   Appointments: {len(all_appts)} total ({len(new_appts)} desta semana)")
    print(f"   Semanas Gustavo: {len(merged['people']['gustavo']['weeks'])}")
    print(f"   Semanas Laura:   {len(merged['people']['laura']['weeks'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
