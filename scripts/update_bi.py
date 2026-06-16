#!/usr/bin/env python3
"""
Atualiza o BI de reunioes Smiller com dados do Acuity Scheduling.
Roda via GitHub Actions.
"""

import os
import re
import json
import datetime
import sys
import requests

ACUITY_USER_ID = os.environ["ACUITY_USER_ID"]
ACUITY_API_KEY = os.environ["ACUITY_API_KEY"]
GUSTAVO_CAL    = "14153461"
LAURA_CAL      = "13908391"
ACUITY_BASE    = "https://acuityscheduling.com/api/v1"


def semana(offset):
    hoje = datetime.date.today()
    seg  = hoje - datetime.timedelta(days=hoje.weekday()) + datetime.timedelta(weeks=offset)
    sex  = seg + datetime.timedelta(days=4)
    lbl  = lambda d: d.strftime("%d/%m")
    return {"min": seg.isoformat(), "max": sex.isoformat(),
            "label": f"{lbl(seg)}-{lbl(sex)}", "mes": seg.strftime("%Y-%m")}


def acuity_get(path, params=None):
    r = requests.get(f"{ACUITY_BASE}{path}", params=params,
                     auth=(ACUITY_USER_ID, ACUITY_API_KEY), timeout=30)
    r.raise_for_status()
    return r.json()


def week_counts(cal_id, semanas):
    weeks = []
    for s in semanas:
        p = {"calendarID": cal_id, "minDate": s["min"], "maxDate": s["max"], "max": 200}
        realized = len(acuity_get("/appointments", p))
        canceled = len(acuity_get("/appointments", {**p, "canceled": "true"}))
        weeks.append({"label": s["label"], "mes": s["mes"],
                      "realized": realized, "canceled": canceled, "noshow": 0})
        print(f"  {s['label']}: {realized}r / {canceled}c")
    return weeks


def parse_appt(appt, prof):
    cx = next((f for f in (appt.get("forms") or []) if f["name"] == "Forms CX"), None)
    payload = paciente = ""
    poronde = "-"
    if cx:
        for v in cx.get("values", []):
            if v["name"] == "Payload":   payload  = v.get("value", "")
            elif v["name"] == "Paciente": paciente = v.get("value", "")
            elif v["name"] == "PorOnde":  poronde  = v.get("value", "") or "-"

    def get(label):
        m = re.search(label + r":\s*([^\n]+)", payload)
        return m.group(1).strip() if m else ""

    dt_str = re.sub(r"([+-])(\d{2})(\d{2})$", r"\1\2:\3", appt["datetime"])
    dt = datetime.datetime.fromisoformat(dt_str)
    data_str = dt.strftime("%Y-%m-%d")
    dias = ["Seg","Ter","Qua","Qui","Sex","Sab","Dom"]
    dia_str = f"{dias[dt.weekday()]} {dt.day:02d}/{dt.month:02d}"

    return {"data": data_str, "mes": data_str[:7], "prof": prof,
            "tipo": appt.get("type",""), "dia": dia_str,
            "cliente": f"{appt.get('firstName','')} {appt.get('lastName','')}",
            "status": get("Status do tratamento"),
            "mecanica": get("Mecanica auxiliar") or get(r"Mec[aâ]nica auxiliar"),
            "suporte": get("Suporte anterior da equipe"),
            "complexidade": get("Complexidade do caso"),
            "agenda": get("Agenda"), "paciente": paciente, "poronde": poronde}


def week_appts(cal_id, prof, s):
    appts = acuity_get("/appointments",
        {"calendarID": cal_id, "minDate": s["min"], "maxDate": s["max"], "max": 200})
    return [parse_appt(a, prof) for a in appts]


def extract_data_block(html):
    marker = "const DATA ="
    start = html.find(marker)
    if start == -1: raise ValueError("Bloco DATA nao encontrado")
    i = start + len(marker)
    while i < len(html) and html[i] != "{": i += 1
    depth = end = 0
    for j in range(i, len(html)):
        if html[j] == "{": depth += 1
        elif html[j] == "}":
            depth -= 1
            if depth == 0: end = j; break
    js = html[i:end+1]
    js = re.sub(r'(?<!["\'\\w])(\b\w+\b)\s*:', r'"\1":', js)
    js = re.sub(r",(\s*[}\]])", r"\1", js)
    return json.loads(js), start + len(marker), end


def norm_label(l):
    return re.sub(r"[\u2013\u2014\-\s]", "", l).lower()


def merge_weeks(existing, new):
    m = {norm_label(w["label"]): dict(w) for w in existing}
    for w in new:
        k = norm_label(w["label"])
        if k in m:
            m[k]["realized"] = w["realized"]
            if w["canceled"] > 0: m[k]["canceled"] = w["canceled"]
        else: m[k] = dict(w)
    return sorted(m.values(), key=lambda w: w["mes"] + w["label"])


def esc(v):
    return str(v or "").replace("\\", "\\\\").replace('"', '\\"')


def build_data_block(merged):
    def weeks_js(weeks):
        return ",\n".join(
            f'        {{ label:"{w["label"]}", mes:"{w["mes"]}", realized:{w["realized"]}, canceled:{w["canceled"]}, noshow:{w["noshow"]} }}'
            for w in weeks)
    def appts_js(appts):
        return ",\n".join(
            f'      {{data:"{esc(a["data"])}",mes:"{esc(a["mes"])}",prof:"{esc(a["prof"])}",tipo:"{esc(a["tipo"])}",dia:"{esc(a["dia"])}",cliente:"{esc(a["cliente"])}",status:"{esc(a["status"])}",mecanica:"{esc(a["mecanica"])}",suporte:"{esc(a["suporte"])}",complexidade:"{esc(a["complexidade"])}",agenda:"{esc(a["agenda"])}",paciente:"{esc(a["paciente"])}",poronde:"{esc(a["poronde"])}"}}'
            for a in appts)
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


def main():
    print("=== BI Smiller - Atualizacao automatica ===")
    semanas      = [semana(i) for i in range(-4, 1)]
    semana_atual = semanas[-1]
    print(f"Semana atual: {semana_atual['min']} -> {semana_atual['max']}")

    print("\n Gustavo Bernardes:")
    g_weeks = week_counts(GUSTAVO_CAL, semanas)
    print("\n Laura Raposo:")
    l_weeks = week_counts(LAURA_CAL, semanas)

    print("\nBuscando appointments detalhados...")
    g_appts = week_appts(GUSTAVO_CAL, "Gustavo", semana_atual)
    l_appts = week_appts(LAURA_CAL,   "Laura",   semana_atual)
    print(f"  Gustavo: {len(g_appts)} | Laura: {len(l_appts)}")

    print("\nLendo index.html...")
    with open("index.html", encoding="utf-8") as f:
        html = f.read()
    existing_data, data_start, data_end = extract_data_block(html)

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
            "gustavo": {**existing_data["people"]["gustavo"],
                        "weeks": merge_weeks(existing_data["people"]["gustavo"]["weeks"], g_weeks)},
            "laura":   {**existing_data["people"]["laura"],
                        "weeks": merge_weeks(existing_data["people"]["laura"]["weeks"], l_weeks)},
        },
        "appointments": all_appts,
    }

    new_block = build_data_block(merged)
    new_html  = html[:data_start] + " " + new_block + html[data_end + 1:]
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(new_html)

    print(f"\n index.html atualizado:")
    print(f"   Appointments: {len(all_appts)} total ({len(new_appts)} desta semana)")
    print(f"   Semanas Gustavo: {len(merged['people']['gustavo']['weeks'])}")
    print(f"   Semanas Laura:   {len(merged['people']['laura']['weeks'])}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
