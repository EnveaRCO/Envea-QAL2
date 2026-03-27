"""
pdf_parser.py — Extraction complète des données QAL2
Utilise pypdf (léger, compatible Render free tier).
Références : NF EN 14181, XP X43-132, NF EN 15259
"""
import re
import io
from typing import Optional, List, Dict

try:
    import pypdf
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False


# ─── Aliases polluants ────────────────────────────────────────────────────────

POLLUTANT_ALIASES = {
    "o2":        ["oxygène", "oxygen", "o2", "o₂"],
    "co":        ["monoxyde de carbone", "monoxyde", "co "],
    "nox":       ["oxydes d'azote", "oxyde d'azote", "nox", "no2", "no₂", "nox eq"],
    "covt":      ["composés organiques volatils", "covt", "cov total", "voc", "cov "],
    "poussieres":["poussières", "poussieres", "dust", "particule"],
    "so2":       ["dioxyde de soufre", "so2", "so₂"],
    "hcl":       ["chlorure d'hydrogène", "chlorure", "hcl"],
    "hf":        ["fluorure d'hydrogène", "fluorure", "hf "],
    "hg":        ["mercure", "mercury", "hg "],
    "nh3":       ["ammoniac", "ammonia", "nh3", "nh₃"],
    "h2o":       ["humidité", "eau", "h2o", "teneur en eau"],
    "co2":       ["dioxyde de carbone", "co2", "co₂"],
}

CAS_MAP = {
    "a": "Cas A — régression standard (concentration > 30% VLE, plage > 15% VLE)",
    "b": "Cas B — droite forcée zéro (plage < 15% VLE)",
    "c": "Cas C — combinaison mesures + MR (concentration < 30% VLE)",
}

# σ0 per compound in % (XP X43-132 Tableau 1)
SIGMA0_DEFAULT = {
    "poussieres": 30.0,
    "hcl": 40.0,
    "hf": 40.0,
    "so2": 20.0,
    "nox": 20.0,
    "covt": 30.0,
    "co": 10.0,
    "o2": 15.0,
    "hg": 30.0,
    "nh3": 40.0,
}

# Response time limits per compound (seconds) — XP X43-132 §5.4
RESPONSE_TIME_LIMIT = {
    "hcl": 400, "hf": 400, "nh3": 400, "hg": 400,
}
RESPONSE_TIME_DEFAULT_LIMIT = 200

# ─── Extraction principale ─────────────────────────────────────────────────────

def parse_pdf(file_bytes: bytes) -> dict:
    """Extrait toutes les données utiles d'un rapport QAL2 PDF."""
    if not HAS_PYPDF:
        return {"error": "pypdf non installé", "raw_text": "", "pollutants": [], "meta": {}}

    result = {
        "meta": {},
        "pollutants": [],
        "operational_tests": {},
        "response_times": [],
        "derogations": [],
        "raw_text": "",
        "pages": 0,
        "parse_confidence": 0,
        "warnings": [],
    }

    try:
        reader = pypdf.PdfReader(io.BytesIO(file_bytes))
        result["pages"] = len(reader.pages)
        all_text = ""
        page_texts = []
        for page in reader.pages:
            t = page.extract_text() or ""
            page_texts.append(t)
            all_text += t + "\n"

        result["raw_text"] = all_text

        result["meta"]             = _extract_metadata(all_text, page_texts)
        result["pollutants"]       = _extract_pollutants(all_text, page_texts)
        result["operational_tests"]= _extract_operational_tests(all_text)
        result["response_times"]   = _extract_response_times(all_text)
        result["derogations"]      = _extract_derogations(all_text)

        # Score de confiance
        meta = result["meta"]
        confidence = 0
        if meta.get("dossier"):   confidence += 20
        if meta.get("client"):    confidence += 15
        if meta.get("labo"):      confidence += 15
        if meta.get("dates"):     confidence += 10
        if len(result["pollutants"]) >= 3: confidence += 30
        elif len(result["pollutants"]) > 0: confidence += 15
        if result["response_times"]:  confidence += 5
        if result["operational_tests"]: confidence += 5
        result["parse_confidence"] = min(confidence, 100)

    except Exception as e:
        result["error"] = str(e)

    return result


# ─── Extraction métadonnées ────────────────────────────────────────────────────

def _extract_metadata(text: str, pages: list) -> dict:
    meta = {
        "dossier": None, "client": None, "labo": None, "labo_cofrac": None,
        "installation": None, "ville": None, "dates": None, "nb_essais": None,
        "type_rapport": "QAL2", "emission_date": None, "intervenants": [],
        "ams_titulaire": None, "ams_redundant": None,
    }

    # Référence dossier
    for pat in [
        r'\b([A-Z]{2,5}\d{2}-[A-Z]\d{3}-PR\d{2}-V\d{2})\b',
        r'rapport[^:]*[:]\s*([A-Z]{2,6}[\d]{2,4}-[A-Z0-9\-]+)',
        r'n°\s*([A-Z]{2,6}[\d\-]+[A-Z]{1,4}[\d\-]+)',
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            meta["dossier"] = m.group(1).strip()
            break

    # Client / site
    for pat in [
        r'(?:client|commanditaire|demandeur)\s*[:\-]\s*([^\n]{3,60})',
        r'site\s+(?:de\s+)?([A-Z][A-Za-zÀ-ÿ\s]{3,40})',
        r'installation[^:]*[:]\s*([^\n]{5,60})',
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            v = m.group(1).strip().rstrip(',.:')
            if 2 < len(v) < 80:
                meta["client"] = _clean_value(v)
                break

    # Laboratoire
    for pat in [
        r"(KALI'AIR|KALI.AIR|SOCOTEC|BUREAU VERITAS|APAVE|EUROFINS AIR|INERIS|AIR LORRAINE|CERE)",
        r'laboratoire\s*[:\-]\s*([^\n]{3,50})',
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            meta["labo"] = _clean_value(m.group(1)).strip()
            break

    # COFRAC n°
    m = re.search(r'accr[eé]ditation[^\d]*(\d{1,2}-\d{4})', text, re.IGNORECASE)
    if m:
        meta["labo_cofrac"] = m.group(1)

    # Installation / type source
    for pat in [
        r'(four de [a-zé]+|chaudière|incinér\w+|four de brulage|turbine gaz|moteur\s+\w+)',
        r'[Ff]our\s+[-–]\s+([^\n]{3,40})',
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            meta["installation"] = _clean_value(m.group(1)).upper()
            break

    # Ville
    for pat in [
        r'\b(\d{5})\s+([A-ZÉÀÂ][A-Za-zÀ-ÿ\-]{2,30})',
        r'(?:commune de|à)\s+([A-ZÉÀÂ][A-Za-zÀ-ÿ\-\s]{2,30})',
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            v = (m.group(2) if r'\d{5}' in pat else m.group(1)).strip().rstrip(',.')
            if 2 < len(v) < 40:
                meta["ville"] = v.upper()
                break

    # Dates
    for pat in [
        r"(\d{1,2}[./]\d{1,2}[./]\d{4})\s+[àau]+\s+(\d{1,2}[./]\d{1,2}[./]\d{4})",
        r"du\s+(\d{1,2}[^\n]{5,30}\d{4})",
        r"(\d{1,2}\s+\w+\s+\d{4})\s+au\s+(\d{1,2}\s+\w+\s+\d{4})",
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            meta["dates"] = _clean_value(m.group(1))
            if m.lastindex >= 2:
                meta["dates"] = _clean_value(m.group(1)) + " au " + _clean_value(m.group(2))
            break

    # Nombre d'essais (total sur le rapport, cherche le max)
    nb_max = 0
    for m in re.finditer(r"nombre d'essais valides[^\d]*(\d+)", text, re.IGNORECASE):
        v = int(m.group(1))
        if v > nb_max:
            nb_max = v
    if nb_max > 0:
        meta["nb_essais"] = nb_max

    # Type rapport
    if re.search(r'\bAST\b', text[:500]):
        meta["type_rapport"] = "AST"

    # AMS identifiés
    for pat in [r'(ENVEA\s+\w+[\s\w\-]+)(?:\s+\(|,|\n)', r'(MIR\s*9000|PCME\s+QAL|Graphite\s+52)', r'(analyseur\s+[A-Z]\w+)', ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            meta["ams_titulaire"] = _clean_value(m.group(1))
            break

    return meta


# ─── Extraction polluants ──────────────────────────────────────────────────────

def _extract_pollutants(text: str, pages: list) -> list:
    """Extrait les données par polluant depuis le rapport."""
    # Méthode 1: Sections individuelles par polluant (plus fiable)
    pollutants = _parse_per_pollutant(text, pages)

    # Méthode 2: Table synthèse si méthode 1 insuffisante
    if len(pollutants) < 2:
        pollutants = _parse_synthesis_table(text)

    return pollutants


SECTION_HEADERS = [
    (r"Oxygène\s*[-–]\s*O[₂2]",                        "o2"),
    (r"Monoxyde de [Cc]arbone\s*[-–]\s*CO\b",           "co"),
    (r"[Oo]xydes? d.azote\s*[-–]\s*NO[xX]",            "nox"),
    (r"[Cc]omposés? [Oo]rganiques? [Vv]olatils?\s+[-–]?\s*COV[tT]", "covt"),
    (r"[Pp]oussières? totales?",                         "poussieres"),
    (r"[Dd]ioxyde de [Ss]oufre\s*[-–]\s*SO[₂2]",       "so2"),
    (r"[Cc]hlorure d.hydrog.ne\s*[-–]\s*HCl",           "hcl"),
    (r"[Ff]luorure d.hydrog.ne\s*[-–]\s*HF\b",          "hf"),
    (r"[Mm]ercure\s*[-–]\s*Hg\b",                       "hg"),
    (r"[Aa]mmoniac\s*[-–]\s*NH[₃3]",                   "nh3"),
]


def _parse_per_pollutant(text: str, pages: list) -> list:
    """Parse les sections individuelles par polluant (détaillées)."""
    pollutants = []
    seen = set()

    # Trouver toutes les occurrences de chaque section polluant dans la partie résultats
    results_start = _find_section(text, [r"10\.\s*R[eé]sultats", r"R[eé]sultats de la campagne"])
    if results_start < 0:
        results_start = len(text) // 2  # fallback: 2ème moitié du document

    for pattern, poll_id in SECTION_HEADERS:
        matches = list(re.finditer(pattern, text, re.IGNORECASE))
        if not matches:
            continue

        # Prendre le match le plus profond dans la section résultats (section 10)
        target = None
        for m in matches:
            if m.start() >= results_start:
                target = m
                break
        if target is None:
            target = matches[-1]  # fallback: dernière occurrence

        if poll_id in seen:
            continue
        seen.add(poll_id)

        start = target.start()
        # Extraire jusqu'au prochain polluant ou 8000 chars
        next_start = len(text)
        for _, _ in SECTION_HEADERS:
            for pat2, _ in SECTION_HEADERS:
                nm = re.search(pat2, text[start+50:start+8000], re.IGNORECASE)
                if nm:
                    ns = start + 50 + nm.start()
                    if ns < next_start and ns > start + 200:
                        next_start = ns

        section = text[start:min(start+8000, next_start)]
        p = _extract_channel_data(section, poll_id)
        if p:
            p["name"]     = poll_id
            p["name_raw"] = target.group(0).strip()
            pollutants.append(p)

    return pollutants


def _parse_synthesis_table(text: str) -> list:
    """Fallback: Parse la table de synthèse globale."""
    pollutants = []
    synth_start = _find_section(text, [r"synthèse.{0,40}fonctions?.+étalonnage", r"résultats.+étalonnage"])
    if synth_start < 0:
        return []

    data_pattern = re.compile(
        r'\b(o2|co\b|nox|covt|cov\s|poussières|poussieres|so2|hcl\b|hf\b|hg\b|nh3)\b',
        re.IGNORECASE
    )

    lines = text[synth_start:synth_start+2000].split('\n')
    for i, line in enumerate(lines):
        dm = data_pattern.search(line)
        if not dm:
            continue
        poll_id = _normalize_pollutant_name(dm.group(1))
        ctx = '\n'.join(lines[i:i+5])
        p = _extract_channel_data(ctx, poll_id)
        if p:
            p["name"] = poll_id
            p["name_raw"] = line.strip()[:50]
            pollutants.append(p)

    return pollutants


def _extract_channel_data(ctx: str, poll_id: str) -> Optional[dict]:
    """Extrait toutes les données d'un canal depuis son contexte texte."""
    result = {}

    # ── Pente a ──
    for pat in [
        r'Pente\s*[:\s=]+\s*a\s*=\s*([-]?\d+[,\.]\d+)',
        r'\ba\s*=\s*([-]?\d+[,\.]\d+)',
        r'a\s*=\s*([-]?\d+)\b',
    ]:
        m = re.search(pat, ctx, re.IGNORECASE)
        if m:
            result['a'] = _to_float(m.group(1))
            break

    # ── Intercept b ──
    for pat in [
        r'[Cc]ste\s*[:\s=]+\s*b\s*=\s*([-]?\d+[,\.]\d+)',
        r'\bb\s*=\s*([-]?\d+[,\.]\d+)',
        r'\bb\s*=\s*([-]?\d+)\b',
    ]:
        m = re.search(pat, ctx, re.IGNORECASE)
        if m:
            result['b'] = _to_float(m.group(1))
            break

    # ── R² ──
    for pat in [r'R2\s*=\s*(\d+[,\.]\d+)', r'R[²2]\s*=\s*(\d+[,\.]\d+)']:
        m = re.search(pat, ctx, re.IGNORECASE)
        if m:
            result['r2'] = _to_float(m.group(1))
            break

    # ── Sd ──
    for pat in [r'[Ee]cart.type\s+Sd\s*\n?\s*([\d,\.]+)', r'Sd\s*\n\s*([\d,\.]+)', r'Ecart.type Sd\s+([\d,\.]+)']:
        m = re.search(pat, ctx, re.IGNORECASE)
        if m:
            result['sd'] = _to_float(m.group(1))
            break
    if 'sd' not in result:
        m = re.search(r'\bSd\b[^\d]{1,10}([\d,\.]+)', ctx, re.IGNORECASE)
        if m:
            result['sd'] = _to_float(m.group(1))

    # ── σ0·Kv (seuil variabilité) ──
    for pat in [
        r'[σs]0?\s*[·*×]\s*[Kk][vV]\s*[=:]\s*([\d,\.]+)',
        r'\bσ\s*[*·]\s*Kv\s*([\d,\.]+)',
        r'σ\s*\*\s*Kv\s*\n\s*([\d,\.]+)',
    ]:
        m = re.search(pat, ctx, re.IGNORECASE)
        if m:
            result['kv_threshold'] = _to_float(m.group(1))
            break

    # ── VLE ──
    m = re.search(r'VLE\s*[=:]\s*([\d,\.]+)', ctx, re.IGNORECASE)
    if m:
        result['vle'] = _to_float(m.group(1))

    # ── Kv ──
    m = re.search(r'Kv=\s*([\d,\.]+)', ctx, re.IGNORECASE)
    if m:
        result['kv'] = _to_float(m.group(1))

    # ── σ0 ──
    m = re.search(r'[Ee]xigence\s+p\s*\(%\)\s*=\s*([\d,\.]+)', ctx, re.IGNORECASE)
    if m:
        result['sigma0'] = _to_float(m.group(1))
    else:
        result['sigma0'] = SIGMA0_DEFAULT.get(poll_id)

    # ── Cas XP X43-132 ──
    cas_m = re.search(r'(?:proc[eé]dure\s+appliqu[ée]+|cas\s+[àa]\s+appliquer)\s*[:\s]+cas\s+([ABC])\b', ctx, re.IGNORECASE)
    if not cas_m:
        cas_m = re.search(r'\bcas\s+([ABC])\s+(?:selon|appliqu|à)', ctx, re.IGNORECASE)
    if not cas_m:
        cas_m = re.search(r'[Ss]tratégie de mesur\w+\s*[:\s]+cas\s+([ABC])\b', ctx, re.IGNORECASE)
    if cas_m:
        result['cas'] = cas_m.group(1).upper()

    # ── Stratégie (A1/A2/B) ──
    strat_m = re.search(r'stratégie\s*[:\s]+([AB][12]?)\b', ctx, re.IGNORECASE)
    if strat_m:
        result['strategy'] = strat_m.group(1).upper()

    # ── Nombre essais valides ──
    m = re.search(r"nombre d'essais valides[^\d]*(\d+)", ctx, re.IGNORECASE)
    if m:
        result['nb_essais_valides'] = int(m.group(1))

    # ── Nombre essais retirés ──
    m = re.search(r"[Nn]ombre d'essais retirés[^\d]*(\d+)", ctx, re.IGNORECASE)
    if m:
        result['essais_retires'] = int(m.group(1))

    # ── Domaine de validité ──
    m = re.search(r"domaine de validité d.étalonnage est donc\s*[:\s]+[^\d]*([\d,\.]+)\s+[àa]\s+([\d,\.]+)", ctx, re.IGNORECASE)
    if m:
        result['domaine_min'] = _to_float(m.group(1))
        result['domaine_max'] = _to_float(m.group(2))

    # ── Unité ──
    for pat in [r'mg/m[03³]\s*(?:sec|hum|h)?(?:\s+O[₂2]\s+[Rr][eé]f)?', r'%\s*sec', r'mg/Nm\s*[³3]']:
        m = re.search(pat, ctx, re.IGNORECASE)
        if m:
            result['unit'] = m.group(0).strip()
            break

    # ── Verdict ──
    # Chercher le résultat du test de variabilité (plus fiable que "valide" générique)
    if re.search(r'test de variabilité\s+(?:est\s+)?(?:pass[eé]\s+avec\s+succès|réussi|conforme)\b', ctx, re.IGNORECASE):
        result['valid'] = True
    elif re.search(r'test de variabilité\s+(?:est\s+)?en\s+échec\b', ctx, re.IGNORECASE):
        result['valid'] = False
    elif re.search(r'\bvalide\b', ctx, re.IGNORECASE) and not re.search(r'\binvalide\b|\bnon\s+valide\b', ctx, re.IGNORECASE):
        result['valid'] = True
    elif re.search(r'\binvalide\b|non\s+valide\b', ctx, re.IGNORECASE):
        result['valid'] = False

    # ── Dérogations spécifiques canal ──
    result['derogation'] = None
    if re.search(r'déroga|la VLE est donc remplacée|moins de 5 couples', ctx, re.IGNORECASE):
        m = re.search(r'Remarque\s*[:\s]+([^\n]{10,200})', ctx, re.IGNORECASE)
        if m:
            result['derogation'] = _clean_value(m.group(1))
        else:
            result['derogation'] = "Dérogation appliquée (voir rapport)"

    if 'a' not in result:
        return None
    return result


# ─── Extraction Tests Opérationnels ───────────────────────────────────────────

def _extract_operational_tests(text: str) -> dict:
    """Extrait les résultats des sections 9.x - Tests Opérationnels."""
    ops = {}

    # ── 9.1 Alignement et propreté ──
    sec91 = _extract_section(text, r"9\.1", r"9\.2")
    if sec91:
        items = re.findall(r'([A-Za-zÀ-ÿ\s]{10,60})\s+(Effectué|Non effectué)', sec91, re.IGNORECASE)
        ops["alignement"] = {
            "present": True,
            "items": [{"label": i[0].strip(), "status": i[1].lower()} for i in items],
            "all_ok": all(i[1].lower() == "effectué" for i in items) if items else None,
        }

    # ── 9.2 Système de prélèvement ──
    sec92 = _extract_section(text, r"9\.2", r"9\.3")
    if sec92:
        items = re.findall(r'([A-Za-zÀ-ÿ\s,]{10,60})\s+(Satisfaisant|Non satisfaisant)', sec92, re.IGNORECASE)
        ops["systeme_prelevement"] = {
            "present": True,
            "items": [{"label": i[0].strip(), "status": i[1].lower()} for i in items],
            "all_ok": all(i[1].lower() == "satisfaisant" for i in items) if items else None,
        }

    # ── 9.3 Documentation ──
    sec93 = _extract_section(text, r"9\.3", r"9\.4")
    if sec93:
        docs = {}
        doc_items_raw = re.findall(
            r'([A-Za-zÀ-ÿ\'"\(\)\s\-\,]{15,80})\s+(OUI|NON)\b',
            sec93, re.IGNORECASE
        )
        for label, status in doc_items_raw:
            label_clean = _clean_value(label).lower()
            # Categorise
            if "fiche de suivi" in label_clean:
                docs["fiche_suivi"] = status.upper()
            elif "diagramme" in label_clean or "synoptique" in label_clean:
                docs["diagrammes"] = status.upper()
            elif "schéma" in label_clean:
                docs["schema"] = status.upper()
            elif "manuel" in label_clean:
                docs["manuels"] = status.upper()
            elif "traitement du signal" in label_clean:
                docs["traitement_signal"] = status.upper()
            elif "qal3" in label_clean.replace(" ", ""):
                docs["qal3"] = status.upper()
            elif "procédure" in label_clean and "gestion" in label_clean:
                docs["procedures_maintenance"] = status.upper()
            elif "responsable" in label_clean:
                docs["responsable"] = status.upper()
            elif "formation" in label_clean:
                docs["formation"] = status.upper()
            elif "planning de maintenance" in label_clean:
                docs["planning_maintenance"] = status.upper()
            elif "planning d'audit" in label_clean or "audit" in label_clean:
                docs["planning_audit"] = status.upper()
            elif "maintenance" in label_clean and "rapport" in label_clean:
                docs["rapports_maintenance"] = status.upper()
            elif "cahier" in label_clean:
                docs["cahier_suivi"] = status.upper()

        nb_non = sum(1 for v in docs.values() if v == "NON")
        ops["documentation"] = {
            "present": True,
            "items": docs,
            "nb_manquant": nb_non,
            "all_ok": nb_non == 0,
        }

    # ── 9.4 Aptitude à l'emploi ──
    sec94 = _extract_section(text, r"9\.4", r"9\.5")
    if sec94:
        aptitude_items = re.findall(
            r'([A-Za-zÀ-ÿ\s\(\),\']{15,100})\s+(OUI|NON)\b',
            sec94, re.IGNORECASE
        )
        items_clean = []
        for label, status in aptitude_items:
            items_clean.append({"label": _clean_value(label), "status": status.upper()})
        nb_non = sum(1 for i in items_clean if i["status"] == "NON")
        ops["aptitude"] = {
            "present": True,
            "items": items_clean,
            "nb_non_conforme": nb_non,
            "all_ok": nb_non == 0,
        }

    # ── 9.6 Étanchéité ──
    sec96 = _extract_section(text, r"9\.6", r"9\.7")
    if sec96:
        conforme = bool(re.search(r'r[eé]sultats?\s+[Oo]btenus[^\n]*OUI|conforme[^\n]*OUI', sec96, re.IGNORECASE))
        non_conforme = bool(re.search(r'r[eé]sultats?\s+[Oo]btenus[^\n]*NON', sec96, re.IGNORECASE))
        ops["etancheite"] = {
            "present": True,
            "conforme": conforme and not non_conforme,
            "status": "CONFORME" if (conforme and not non_conforme) else ("NON CONFORME" if non_conforme else "?"),
        }

    # ── 9.7 Zero et Sensibilité ──
    sec97 = _extract_section(text, r"9\.7", r"9\.8")
    if sec97:
        ops["zero_sensibilite"] = {
            "present": True,
            "nb_injections": 3,  # Standard: 3 zeros + 3 sensibilités
            "note": "3 zéros + 3 sensibilités réalisés pour chaque gaz",
        }

    # ── 9.8 MR Non-extractifs ──
    sec98 = _extract_section(text, r"9\.8", r"9\.9")
    if sec98:
        aucun = bool(re.search(r"aucun contrôle n.a pu", sec98, re.IGNORECASE))
        ops["mr_non_extractif"] = {
            "present": True,
            "realise": not aucun,
            "note": "Aucun contrôle réalisé lors de cette campagne." if aucun else "Contrôle réalisé.",
        }

    return ops


# ─── Extraction Temps de réponse ──────────────────────────────────────────────

def _extract_response_times(text: str) -> list:
    """Extrait les temps de réponse par paramètre (section 9.9.1)."""
    times = []

    # Trouver la section 9.9
    sec99 = _extract_section(text, r"9\.9", r"10\.")
    if not sec99:
        sec99 = _extract_section(text, r"[Tt]emps de r[eé]ponse", r"10\.")
    if not sec99:
        return []

    # Note injection directe
    direct_injection = bool(re.search(r"HCl et HF inject[eé].{0,30}direct.{0,30}analyseur", sec99, re.IGNORECASE))

    # Parser ligne par ligne
    # Pattern: Parametre | 90% Conc | Temps mesuré | Conformité | Commentaires
    param_map = {
        r'\bO[₂2]\b': 'o2', r'\bCO\b': 'co', r'\bNO.*NO[₂2]\b': 'nox',
        r'\bSO[₂2]\b': 'so2', r'\bC[₃3]H[₈8]\b': 'c3h8', r'\bCH[₄4]\b': 'ch4',
        r'\bHCl\b': 'hcl', r'\bHF\b': 'hf',
    }

    lines = sec99.split('\n')
    for line in lines:
        # Chercher une ligne avec un paramètre connu, une valeur numérique et conformité
        poll_id = None
        for pat, pid in param_map.items():
            if re.search(pat, line, re.IGNORECASE):
                poll_id = pid
                break
        if not poll_id:
            continue

        # Extraire temps de réponse
        time_m = re.search(r'(\d+)\s*secondes?', line, re.IGNORECASE)
        time_min_m = re.search(r"(\d+)['']\s*(?:pour|min)", line, re.IGNORECASE)

        time_s = None
        if time_m:
            time_s = int(time_m.group(1))
        elif time_min_m:
            time_s = int(time_min_m.group(1)) * 60

        # Conformité
        conforme = bool(re.search(r'\bCONFORME\b', line, re.IGNORECASE) and not re.search(r'NON\s+CONFORME', line, re.IGNORECASE))
        non_conforme = bool(re.search(r'NON\s+CONFORME', line, re.IGNORECASE))

        if time_s is not None or non_conforme:
            limit = RESPONSE_TIME_LIMIT.get(poll_id, RESPONSE_TIME_DEFAULT_LIMIT)
            times.append({
                "param": poll_id,
                "time_s": time_s,
                "limit_s": limit,
                "conforme": conforme and not non_conforme,
                "non_conforme": non_conforme,
                "direct_injection": direct_injection and poll_id in ("hcl", "hf"),
                "comments": "",
            })

    # Si pas de parsing ligne par ligne, chercher globalement
    if not times:
        for pat, pid in param_map.items():
            region = re.search(pat + r'.{0,200}', sec99, re.IGNORECASE | re.DOTALL)
            if region:
                seg = region.group(0)
                time_m = re.search(r'(\d+)\s*secondes?', seg)
                non_conf = bool(re.search(r'NON\s+CONFORME', seg, re.IGNORECASE))
                conf = bool(re.search(r'\bCONFORME\b', seg, re.IGNORECASE) and not non_conf)
                if time_m or non_conf:
                    times.append({
                        "param": pid,
                        "time_s": int(time_m.group(1)) if time_m else None,
                        "limit_s": RESPONSE_TIME_LIMIT.get(pid, RESPONSE_TIME_DEFAULT_LIMIT),
                        "conforme": conf,
                        "non_conforme": non_conf,
                        "direct_injection": direct_injection and pid in ("hcl", "hf"),
                        "comments": "",
                    })

    return times


# ─── Extraction Dérogations ────────────────────────────────────────────────────

def _extract_derogations(text: str) -> list:
    """Extrait toutes les dérogations mentionnées dans le rapport."""
    derogations = []

    patterns = [
        r'[Dd][eé]rogation[^\n]*\n([^\n]{5,200})',
        r'[Cc]eci constitue une dérogation.{0,100}',
        r'[Rr]emarque\s*[:\s]+[^\n]{10,200}d[eé]rog\w+[^\n]*',
        r'la VLE est donc remplacée[^\n]+',
        r'moins de 5 couples[^\n]+',
        r'données?.+mg/Nm3.{0,80}humid.{0,80}sans correction',
        r'utilisation.{0,50}fichiers.{0,50}minutes',
    ]

    for pat in patterns:
        for m in re.finditer(pat, text, re.IGNORECASE | re.DOTALL):
            txt = _clean_value(m.group(0)[:250])
            if txt and txt not in derogations:
                derogations.append(txt)

    return derogations[:10]  # Max 10


# ─── Utilitaires ──────────────────────────────────────────────────────────────

def _find_section(text: str, *patterns) -> int:
    """Cherche la première position d'une section parmi plusieurs patterns."""
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.start()
    return -1


def _extract_section(text: str, start_pat: str, end_pat: str, max_chars: int = 4000) -> Optional[str]:
    """Extrait le texte entre deux patterns de section."""
    start_m = re.search(start_pat, text, re.IGNORECASE)
    if not start_m:
        return None
    start = start_m.start()
    end_m = re.search(end_pat, text[start+20:], re.IGNORECASE)
    end = start + 20 + end_m.start() if end_m else start + max_chars
    return text[start:min(end, start + max_chars)]


def _normalize_pollutant_name(raw: str) -> str:
    raw_lower = raw.lower().strip()
    for key, aliases in POLLUTANT_ALIASES.items():
        for alias in aliases:
            if alias in raw_lower:
                return key
    return raw_lower.replace(' ', '_')


def _to_float(s) -> Optional[float]:
    try:
        return float(str(s).replace(',', '.').strip())
    except Exception:
        return None


def _clean_value(s: str) -> str:
    return re.sub(r'\s+', ' ', str(s)).strip()
