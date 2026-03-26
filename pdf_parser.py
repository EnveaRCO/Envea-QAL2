"""
pdf_parser.py — Extraction intelligente des données QAL2
Supporte les formats KALI'AIR, SOCOTEC, Bureau Veritas, Apave, Eurofins
"""
import re
import io
from typing import Optional

try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except ImportError:
    HAS_PDFPLUMBER = False


# ─── Patterns normatifs ────────────────────────────────────────────────────────

POLLUTANT_ALIASES = {
    "o2": ["oxygène", "oxygen", "o2", "o₂"],
    "co": ["monoxyde de carbone", "monoxyde", "co "],
    "nox": ["oxydes d'azote", "oxyde d'azote", "nox", "no2", "no₂", "nox eq"],
    "covt": ["composés organiques volatils", "covt", "cov total", "voc", "cov "],
    "poussieres": ["poussières", "poussieres", "dust", "particule"],
    "so2": ["dioxyde de soufre", "so2", "so₂"],
    "hcl": ["chlorure d'hydrogène", "chlorure", "hcl"],
    "hf": ["fluorure d'hydrogène", "fluorure", "hf "],
    "hg": ["mercure", "mercury", "hg "],
    "nh3": ["ammoniac", "ammonia", "nh3", "nh₃"],
    "h2o": ["humidité", "eau", "h2o", "teneur en eau"],
    "co2": ["dioxyde de carbone", "co2", "co₂"],
    "no": ["monoxyde d'azote", "no "],
    "n2o": ["protoxyde d'azote", "n2o"],
}

STRAT_MAP = {
    "a1": "A1 — AMS avec QAL1 (NF EN 14181 §4.2)",
    "a2": "A2 — AMS sans QAL1 (NF EN 14181 §4.3)",
    "b": "B — AMS opacimètre/poussières (NF EN 14181 §4.4)",
}

CAS_MAP = {
    "a": "Cas A — régression standard (concentration > 30% VLE, plage > 15% VLE)",
    "b": "Cas B — droite forcée zéro (plage < 15% VLE)",
    "c": "Cas C — combinaison mesures + MR (concentration < 30% VLE)",
}

# ─── Extraction principale ─────────────────────────────────────────────────────

def parse_pdf(file_bytes: bytes) -> dict:
    """Extrait toutes les données utiles d'un rapport QAL2 PDF."""
    if not HAS_PDFPLUMBER:
        return {"error": "pdfplumber non installé", "raw_text": ""}

    result = {
        "meta": {},
        "pollutants": [],
        "raw_text": "",
        "pages": 0,
        "parse_confidence": 0,
        "warnings": [],
    }

    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            result["pages"] = len(pdf.pages)
            all_text = ""
            page_texts = []
            for page in pdf.pages:
                t = page.extract_text() or ""
                page_texts.append(t)
                all_text += t + "\n"

            result["raw_text"] = all_text

            # Extraction métadonnées
            result["meta"] = _extract_metadata(all_text, page_texts)

            # Extraction polluants
            result["pollutants"] = _extract_pollutants(all_text, page_texts)

            # Score de confiance
            meta = result["meta"]
            confidence = 0
            if meta.get("dossier"): confidence += 20
            if meta.get("client"): confidence += 20
            if meta.get("labo"): confidence += 15
            if meta.get("dates"): confidence += 15
            if len(result["pollutants"]) > 0: confidence += 30
            result["parse_confidence"] = min(confidence, 100)

    except Exception as e:
        result["error"] = str(e)

    return result


# ─── Extraction métadonnées ────────────────────────────────────────────────────

def _extract_metadata(text: str, pages: list) -> dict:
    meta = {
        "dossier": None,
        "client": None,
        "labo": None,
        "labo_cofrac": None,
        "installation": None,
        "ville": None,
        "dates": None,
        "nb_essais": None,
        "type_rapport": "QAL2",
        "emission_date": None,
        "intervenants": [],
    }

    # Référence dossier (ex: CKL25-A593-PR01-V01 ou BV-2025-XXX)
    ref_patterns = [
        r'rapport[^:]*[:]\s*([A-Z]{2,6}[\d]{2,4}-[A-Z0-9\-]+)',
        r'référen[cé]+[^:]*[:]\s*([A-Z]{2,6}[\d]{2,4}-[A-Z0-9\-]+)',
        r'\b([A-Z]{2,5}\d{2}-[A-Z]\d{3}-PR\d{2}-V\d{2})\b',
        r'n°\s*([A-Z]{2,6}[\d\-]+[A-Z]{1,4}[\d\-]+)',
    ]
    for pat in ref_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            meta["dossier"] = m.group(1).strip()
            break

    # Client
    client_patterns = [
        r'client\s*[:\-]\s*([^\n]{3,60})',
        r'société\s+([A-Z][A-Za-zÀ-ÿ\s]{2,40})\s+exploite',
        r'commanditaire\s*[:\-]\s*([^\n]{3,50})',
        r'demandeur\s*[:\-]\s*([^\n]{3,50})',
    ]
    for pat in client_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = m.group(1).strip().rstrip(',.:')
            if len(val) > 2 and len(val) < 80:
                meta["client"] = _clean_value(val)
                break

    # Laboratoire
    labo_patterns = [
        r'agence[^:]*[:]\s*([A-Za-zÀ-ÿ\'\s]{2,40}(?:nord|sud|est|ouest|paris|lyon)?)',
        r'laboratoire\s*[:\-]\s*([^\n]{3,50})',
        r"(KALI'AIR|KALI.AIR|SOCOTEC|BUREAU VERITAS|APAVE|EUROFINS|INERIS|AIR LORRAINE)",
        r'accréditation[^:]*[:]\s*[^\n]*\n[^\n]*par\s+([A-Za-zÀ-ÿ\s]{3,40})',
    ]
    for pat in labo_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = m.group(1).strip().rstrip(',.:')
            if len(val) > 2:
                meta["labo"] = _clean_value(val)
                break

    # Numéro COFRAC
    cofrac_m = re.search(r'accr[eé]ditation[^\d]*(\d{1,2}-\d{4})', text, re.IGNORECASE)
    if cofrac_m:
        meta["labo_cofrac"] = cofrac_m.group(1)

    # Installation / Type
    install_patterns = [
        r'(four de [a-zé]+|chaudière|incinér\w+|chaufferie|turbine|moteur|four [a-zé]+)',
        r'installation[^:]*[:]\s*([^\n]{5,80})',
        r"rejet [a\xe0] l.emission\s+([^\n]{5,60})",
    ]
    for pat in install_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            meta["installation"] = _clean_value(m.group(1)).upper()
            break

    # Ville
    ville_patterns = [
        r'(?:commune de|sur la commune de|à|site de)\s+([A-ZÉÀÂ][A-Za-zÀ-ÿ\-\s]{2,40})',
        r'\b(\d{5})\s+([A-ZÉÀÂ][A-Za-zÀ-ÿ\-]{2,30})',
    ]
    for pat in ville_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            v = m.group(2) if r'\d{5}' in pat else m.group(1)
            v = v.strip().rstrip(',.')
            if 2 < len(v) < 40:
                meta["ville"] = v.upper()
                break

    # Dates intervention
    date_patterns = [
        r"(?:dates?[^\:]*:|d'intervention[^\:]*:)\s*du?\s+(\d{1,2}[^\d]+\d{1,2}[^\d]+\d{4})",
        r"du\s+(\d{1,2}\s+(?:au|et)\s+\d{1,2}\s+\w+\s+\d{4})",
        r"(\d{1,2}[./]\d{1,2}[./]\d{4})\s+[àau]+\s+(\d{1,2}[./]\d{1,2}[./]\d{4})",
        r"du\s*(\d{1,2}[^\n]{5,30}\d{4})",
    ]
    for pat in date_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            meta["dates"] = _clean_value(m.group(1))
            break

    # Date émission rapport
    emis_m = re.search(r'[eéE]mis le\s+(\d{1,2}\s+\w+\s+\d{4})', text, re.IGNORECASE)
    if emis_m:
        meta["emission_date"] = emis_m.group(1)

    # Nombre d'essais
    essais_m = re.search(r'(\d+)\s+(?:mesur[a-z]+\s+)?(?:parallèles?\s+)?valid(?:es?|ité)', text, re.IGNORECASE)
    if essais_m:
        meta["nb_essais"] = int(essais_m.group(1))
    else:
        # Compter les lignes du chronogramme
        chrono_count = len(re.findall(r'\b(?:28|29|30|31|01|02|03|04|05|06|07|08|09|10|11|12)-\d{2}-\d{4}\b', text))
        if chrono_count > 0:
            meta["nb_essais"] = min(chrono_count, 18)

    # Type rapport: QAL2 ou AST
    if re.search(r'\bAST\b', text[:500]):
        meta["type_rapport"] = "AST"
    elif re.search(r'\bQAL\s*2\b|\bQAL2\b', text[:500], re.IGNORECASE):
        meta["type_rapport"] = "QAL2"

    # Intervenants
    interv_m = re.findall(r'(?:intervenant|réalisation|ingénieur)[^\n]*[:\-]\s*([A-ZÀ-Ÿ][^\n]{5,60})', text, re.IGNORECASE)
    meta["intervenants"] = [_clean_value(i) for i in interv_m[:4]]

    return meta


# ─── Extraction polluants ──────────────────────────────────────────────────────

def _extract_pollutants(text: str, pages: list) -> list:
    """Extrait la table de synthèse des polluants avec leurs coefficients."""
    pollutants = []

    # Méthode 1: Table de synthèse compact (ligne par polluant)
    pollutants = _parse_synthesis_table(text)

    # Méthode 2: Extraction polluant par polluant si table non trouvée
    if len(pollutants) < 2:
        pollutants = _parse_per_pollutant(text, pages)

    return pollutants


def _parse_synthesis_table(text: str) -> list:
    """Parse la table de synthèse globale du rapport."""
    pollutants = []

    # Chercher les blocs de données de la table de synthèse
    # Pattern: nom_polluant + stratégie + a + b + R² + Sd + threshold + verdict
    lines = text.split('\n')

    # Trouver la section synthèse
    synth_start = -1
    for i, line in enumerate(lines):
        if re.search(r'synthèse|fonction.+étalonnage|composés?.+coefficient', line, re.IGNORECASE):
            synth_start = i
            break

    if synth_start < 0:
        return []

    # Chercher les lignes avec les données numériques
    data_pattern = re.compile(
        r'(o2|co\b|nox|covt|cov\s|poussières|so2|hcl|hf\b|hg\b|nh3)',
        re.IGNORECASE
    )

    # Chercher les patterns numériques dans les lignes de la section synthèse
    # Pattern: a=X.XX ou Y = aX+b ou valeur numérique avec décimales
    float_in_line = re.compile(r'[-]?\d+[,\.]\d+')

    for i, line in enumerate(lines[synth_start:synth_start+60], synth_start):
        dm = data_pattern.search(line)
        if not dm:
            continue
        polluant_raw = dm.group(1).lower().strip()
        polluant_id = _normalize_pollutant_name(polluant_raw)

        # Chercher a, b, R² dans les lignes suivantes
        context = ' '.join(lines[i:i+5])
        p = _extract_coefficients_from_context(context)
        if p:
            p["name"] = polluant_id
            p["name_raw"] = line.strip()[:50]
            pollutants.append(p)

    return pollutants


def _parse_per_pollutant(text: str, pages: list) -> list:
    """Parse les sections individuelles par polluant."""
    pollutants = []

    # Patterns pour trouver les sections de chaque polluant
    section_headers = [
        (r'Oxygène\s*[-–]\s*O[₂2]', 'o2'),
        (r'Monoxyde de [Cc]arbone\s*[-–]\s*CO', 'co'),
        (r"[Oo]xydes? d.azote\s*[-\u2013]\s*NO[xX]", 'nox'),
        (r'[Cc]omposés? [Oo]rganiques?\s+[Vv]olatils?\s*[-–]?\s*COV[tT]?', 'covt'),
        (r'[Pp]oussières?', 'poussieres'),
        (r'[Dd]ioxyde de [Ss]oufre\s*[-–]\s*SO[₂2]', 'so2'),
        (r"[Cc]hlorure d.hydrog.ne\s*[-\u2013]\s*HCl", 'hcl'),
        (r"[Ff]luorure d.hydrog.ne\s*[-\u2013]\s*HF", 'hf'),
        (r'[Mm]ercure\s*[-–]\s*Hg', 'hg'),
        (r'[Aa]mmoniac\s*[-–]\s*NH[₃3]', 'nh3'),
    ]

    for pattern, poll_id in section_headers:
        matches = list(re.finditer(pattern, text, re.IGNORECASE))
        if not matches:
            continue
        # Prendre la première occurrence dans la section synthèse (page 4-7)
        m = matches[0]
        start = m.start()
        section = text[start:start+3000]  # 3000 chars après l'entête

        p = _extract_coefficients_from_context(section)
        if p:
            p["name"] = poll_id
            p["name_raw"] = m.group(0).strip()
            pollutants.append(p)

    return pollutants


def _extract_coefficients_from_context(ctx: str) -> Optional[dict]:
    """Extrait a, b, R², Sd, seuil et verdict d'un bloc de texte."""
    result = {}

    # Pente a
    for pat in [r'a\s*=\s*([-]?\d+[,\.]\d+)', r'pente\s*[:\s=]+\s*([-]?\d+[,\.]\d+)', r'a\s*=\s*([-]?\d+)']:
        m = re.search(pat, ctx, re.IGNORECASE)
        if m:
            result['a'] = _to_float(m.group(1))
            break

    # Intercept b
    for pat in [r'b\s*=\s*([-]?\d+[,\.]\d+)', r'c[ôo]nstante?\s*[:\s=]+\s*([-]?\d+[,\.]\d+)', r'b\s*=\s*([-]?\d+)']:
        m = re.search(pat, ctx, re.IGNORECASE)
        if m:
            result['b'] = _to_float(m.group(1))
            break

    # R²
    for pat in [r'R[²2]\s*=\s*(\d+[,\.]\d+)', r'R\s*carr[eé]\s*=\s*(\d+[,\.]\d+)', r'corr[eé]lation\s*[:\s=]+\s*(\d+[,\.]\d+)']:
        m = re.search(pat, ctx, re.IGNORECASE)
        if m:
            result['r2'] = _to_float(m.group(1))
            break

    # Sd
    for pat in [r'S[dD]\s*=\s*(\d+[,\.]\d+)', r'[eé]cart[^\w]+type\s+S[dD]\s*[=:]\s*(\d+[,\.]\d+)', r'Sd\s+([\d,\.]+)']:
        m = re.search(pat, ctx, re.IGNORECASE)
        if m:
            result['sd'] = _to_float(m.group(1))
            break

    # Seuil 1.5σKv
    for pat in [r'[σs]0?\s*[·*]\s*[Kk][vV]\s*[=<≤]\s*([-]?\d+[,\.]\d+)', r'1[,.]5\s*[σs]\s*[·*]\s*[Kk][vV]\s*([\d,\.]+)',
                r'≤\s+([\d,\.]+)\s+(?:Valid|Invalide|Conforme)']:
        m = re.search(pat, ctx, re.IGNORECASE)
        if m:
            result['kv_threshold'] = _to_float(m.group(1))
            break

    # Verdict
    if re.search(r'\"valide\b(?!\s+valide)', ctx, re.IGNORECASE) and not re.search(r'\binvalide\b|non\s+valide\b', ctx, re.IGNORECASE):
        result['valid'] = True
    elif re.search(r'\binvalide\b|non\s+valide\b|non\s+conforme', ctx, re.IGNORECASE):
        result['valid'] = False

    # Stratégie
    strat_m = re.search(r'stratégie\s*[:\s]+([AB][12]?)\b|(?:^|\s)([AB][12]?)\s+mg|stratégie\s+de\s+mesur\w+\s*[:\s]+\s*([AB][12]?)',
                         ctx, re.IGNORECASE)
    if strat_m:
        s = (strat_m.group(1) or strat_m.group(2) or strat_m.group(3) or '').upper()
        result['strategy'] = s

    # Cas XP X43-132
    cas_m = re.search(r'cas\s+([ABC])\s+(?:selon|à appliquer|:)', ctx, re.IGNORECASE)
    if cas_m:
        result['cas'] = cas_m.group(1).upper()

    # Essais retirés
    retires_m = re.search(r'(?:nombre d\'essais retirés|essais? retirés?)[^\d]*(\d+)', ctx, re.IGNORECASE)
    if retires_m:
        result['essais_retires'] = int(retires_m.group(1))

    # VLE
    vle_m = re.search(r'VLE[^=:\d]*[=:]\s*([\d,\.]+)', ctx, re.IGNORECASE)
    if vle_m:
        result['vle'] = _to_float(vle_m.group(1))

    # Unité
    unit_m = re.search(r'mg/m[03³]?\s*(?:sec|hum|h)?', ctx, re.IGNORECASE)
    if unit_m:
        result['unit'] = unit_m.group(0).strip()
    elif re.search(r'%\s+sec', ctx, re.IGNORECASE):
        result['unit'] = '% sec'

    if not result or 'a' not in result:
        return None
    return result


# ─── Utilitaires ──────────────────────────────────────────────────────────────

def _normalize_pollutant_name(raw: str) -> str:
    raw_lower = raw.lower().strip()
    for key, aliases in POLLUTANT_ALIASES.items():
        for alias in aliases:
            if alias in raw_lower:
                return key
    return raw_lower.replace(' ', '_')


def _to_float(s: str) -> float:
    try:
        return float(str(s).replace(',', '.').strip())
    except:
        return None


def _clean_value(s: str) -> str:
    return re.sub(r'\s+', ' ', s).strip()
