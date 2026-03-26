"""
analyzer.py — Moteur d'analyse normatif QAL2/AST
Références : NF EN 14181, XP X43-132, NF EN ISO/IEC 17025
"""
import re
import math
from typing import Optional

# ─── Critères normatifs ───────────────────────────────────────────────────────

NORM_CRITERIA = {
    "r2_gas_min": 0.9,        # R² minimum gaz (recommandation XP X43-132)
    "r2_dust_min": 0.8,       # R² minimum poussières
    "a_min": 0.9,             # Pente minimum (recommandation XP X43-132)
    "a_max": 1.2,             # Pente maximum
    "b_vle_pct": 0.10,        # Intercept ≤ 10% VLE
    "max_essais_retires": 2,  # Max essais retirés recommandé XP X43-132
    "min_essais_valides": 15, # NF EN 14181 §5.2.1
    "min_jours": 3,           # NF EN 14181 §5.2.1
    "tps_reponse_max": 200,   # secondes (gaz standards)
    "tps_reponse_hcl_hf": 400,# secondes (HCl, HF, NH3, Hg)
}

NORM_VERSIONS = {
    "NF EN 14181": {"current": "2014", "ref": "NF EN 14181:2014"},
    "XP X43-132": {"current": "2015", "ref": "XP X43-132:2015"},
    "NF EN 15259": {"current": "2007", "ref": "NF EN 15259:2007"},
    "NF EN 13284-2": {"current": "2017", "ref": "NF EN 13284-2:2017"},
    "NF EN ISO/IEC 17025": {"current": "2017", "ref": "NF EN ISO/IEC 17025:2017"},
    "NF EN ISO 16911-2": {"current": "2013", "ref": "NF EN ISO 16911-2:2013"},
    "NF EN 14884": {"current": "2015", "ref": "NF EN 14884:2015"},
}

DUST_POLLUTANTS = {"poussieres", "poussières", "dust"}
HCL_HF_NH3_HG = {"hcl", "hf", "nh3", "hg"}
REGULATED_BY_DEFAULT = {"co", "nox", "covt", "poussieres", "so2", "hcl", "hf"}

# ─── Analyse principale ────────────────────────────────────────────────────────

def analyze(parsed_data: dict) -> dict:
    """Analyse complète d'un rapport QAL2 parsé."""
    meta = parsed_data.get("meta", {})
    pollutants = parsed_data.get("pollutants", [])
    raw_text = parsed_data.get("raw_text", "")

    # Analyse rapport
    rapport = _analyze_rapport(meta, pollutants, raw_text)

    # Analyse AMS canal par canal
    ams_results = [_analyze_channel(p, raw_text) for p in pollutants]

    # Comptages
    valid_count = sum(1 for p in ams_results if p.get("valid") is True)
    invalid_count = sum(1 for p in ams_results if p.get("valid") is False)
    regulated_count = sum(1 for p in ams_results if p.get("regulated"))
    regulated_valid = sum(1 for p in ams_results if p.get("valid") and p.get("regulated"))

    # Score AMS
    score_ams = _compute_ams_score(ams_results, regulated_count, regulated_valid)

    # Plan d'action
    actions = _generate_actions(rapport, ams_results, meta)

    return {
        "meta": meta,
        "rapport": rapport,
        "ams_channels": ams_results,
        "score_rapport": rapport["score"],
        "score_ams": score_ams,
        "nb_valid": valid_count,
        "nb_invalid": invalid_count,
        "nb_regulated": regulated_count,
        "nb_regulated_valid": regulated_valid,
        "actions": actions,
        "nb_actions": len(actions),
        "nb_urgent": sum(1 for a in actions if a["priority"] == "P1"),
    }


# ─── Analyse rapport ──────────────────────────────────────────────────────────

def _analyze_rapport(meta: dict, pollutants: list, raw_text: str) -> dict:
    criteres = []
    deductions = 0.0

    # 1. Accréditation COFRAC
    if meta.get("labo_cofrac"):
        criteres.append({
            "label": f"Accréditation COFRAC n°{meta['labo_cofrac']} présente",
            "status": "ok", "points": 0,
            "ref": "NF EN ISO/IEC 17025 §4.1"
        })
    else:
        has_cofrac = bool(re.search(r'accr[eé]ditation|cofrac', raw_text[:2000], re.IGNORECASE))
        if has_cofrac:
            criteres.append({"label": "Mention accréditation COFRAC trouvée (n° non extrait)", "status": "partial", "points": 0, "ref": "NF EN ISO/IEC 17025 §4.1"})
        else:
            criteres.append({"label": "Accréditation COFRAC non mentionnée", "status": "ko", "points": 1.0, "ref": "NF EN ISO/IEC 17025 §4.1"})
            deductions += 1.0

    # 2. Nombre d'essais
    nb = meta.get("nb_essais")
    if nb and nb >= NORM_CRITERIA["min_essais_valides"]:
        criteres.append({"label": f"{nb} essais SRM réalisés (≥ 15 requis)", "status": "ok", "points": 0, "ref": "NF EN 14181 §5.2.1"})
    elif nb:
        criteres.append({"label": f"Seulement {nb} essais SRM (< 15 requis)", "status": "ko", "points": 1.5, "ref": "NF EN 14181 §5.2.1"})
        deductions += 1.5
    else:
        criteres.append({"label": "Nombre d'essais non extrait du rapport", "status": "partial", "points": 0.3, "ref": "NF EN 14181 §5.2.1"})
        deductions += 0.3

    # 3. Tests opérationnels
    has_ops = bool(re.search(r'test\s+opération|alignement|étanchéité|aptitude', raw_text, re.IGNORECASE))
    if has_ops:
        criteres.append({"label": "Tests opérationnels documentés", "status": "ok", "points": 0, "ref": "NF EN 14181 §5.3"})
    else:
        criteres.append({"label": "Tests opérationnels non trouvés dans le rapport", "status": "partial", "points": 0.5, "ref": "NF EN 14181 §5.3"})
        deductions += 0.5

    # 4. Temps de réponse
    tps_m = re.findall(r'(\d+)\s*s[eo]c[oa]ndes?\s*(?:non\s+conforme|NON)', raw_text, re.IGNORECASE)
    tps_non_conf = re.search(r'temps\s+de\s+réponse.{0,200}non\s+conforme', raw_text, re.IGNORECASE | re.DOTALL)
    if tps_non_conf:
        criteres.append({"label": "Temps de réponse non conforme détecté", "status": "ko", "points": 0.5, "ref": "XP X43-132 §5.4 + NF EN 14181 §5.3.5"})
        deductions += 0.5
    else:
        criteres.append({"label": "Temps de réponse conformes (ou non spécifiés)", "status": "ok", "points": 0, "ref": "XP X43-132 §5.4"})

    # 5. Essais retirés > 2 (vérification sur polluants)
    over_retires = [p for p in pollutants if (p.get("essais_retires") or 0) > NORM_CRITERIA["max_essais_retires"]]
    if over_retires:
        names = ", ".join([p.get("name", "?") for p in over_retires])
        criteres.append({"label": f"Essais retirés > 2 sur : {names} (recommandation XP X43-132)", "status": "partial", "points": 0.5, "ref": "XP X43-132 §7.3"})
        deductions += 0.5
    else:
        criteres.append({"label": "Nombre d'essais retirés acceptable (≤ 2)", "status": "ok", "points": 0, "ref": "XP X43-132 §7.3"})

    # 6. Valeur suspecte de seuil variabilité
    zero_threshold = [p for p in pollutants
                      if p.get("kv_threshold") is not None and p.get("kv_threshold") == 0.0]
    if zero_threshold:
        names = ", ".join([p.get("name", "?") for p in zero_threshold])
        criteres.append({"label": f"Seuil variabilité 1.5σKv = 0,0 sur : {names} (anomalie probable)", "status": "ko", "points": 0.5, "ref": "NF EN 14181 §5.4.3"})
        deductions += 0.5

    # 7. Cas / procédure justifiée
    has_cas = bool(re.search(r'cas\s+[abc]\s+(?:selon|:)', raw_text, re.IGNORECASE))
    if has_cas:
        criteres.append({"label": "Procédure d'étalonnage (Cas A/B/C) justifiée", "status": "ok", "points": 0, "ref": "XP X43-132 §7.2"})
    else:
        criteres.append({"label": "Procédure d'étalonnage non explicitée", "status": "partial", "points": 0.2, "ref": "XP X43-132 §7.2"})
        deductions += 0.2

    score = max(1.0, 5.0 - deductions)
    score = round(score * 2) / 2  # arrondi au 0.5

    return {
        "score": score,
        "score_stars": _score_to_stars(score),
        "score_color": _score_color(score),
        "criteres": criteres,
        "anomalies": sum(1 for c in criteres if c["status"] in ("ko", "partial")),
        "errors": sum(1 for c in criteres if c["status"] == "ko"),
    }


# ─── Analyse canal AMS ────────────────────────────────────────────────────────

def _analyze_channel(p: dict, raw_text: str = "") -> dict:
    name = p.get("name", "?")
    result = dict(p)  # copie
    issues = []
    positive = []

    is_dust = name in DUST_POLLUTANTS or "poussiere" in name
    r2_min = NORM_CRITERIA["r2_dust_min"] if is_dust else NORM_CRITERIA["r2_gas_min"]
    regulated = name in REGULATED_BY_DEFAULT
    result["regulated"] = regulated

    a = p.get("a")
    b = p.get("b")
    r2 = p.get("r2")
    sd = p.get("sd")
    kv = p.get("kv_threshold")
    vle = p.get("vle")
    essais_retires = p.get("essais_retires", 0) or 0

    # Score de départ: 5
    score = 5.0
    verdict_override = p.get("valid")  # si le rapport le dit explicitement

    # R²
    if r2 is not None:
        if r2 >= r2_min:
            positive.append({"label": f"R² = {r2} ≥ {r2_min} (critère respecté)", "ref": "XP X43-132 recommandation"})
        elif r2 >= r2_min - 0.05:
            issues.append({"label": f"R² = {r2} légèrement inférieur au seuil recommandé {r2_min}", "severity": "warn", "ref": "XP X43-132 recommandation"})
            score -= 0.5
        else:
            issues.append({"label": f"R² = {r2} insuffisant (seuil recommandé ≥ {r2_min})", "severity": "error", "ref": "XP X43-132 recommandation"})
            score -= 1.5

    # Pente a
    if a is not None:
        if NORM_CRITERIA["a_min"] <= a <= NORM_CRITERIA["a_max"]:
            positive.append({"label": f"Pente a = {a} (0,9 ≤ a ≤ 1,2 ✓)", "ref": "XP X43-132 recommandation"})
        elif a < 0:
            issues.append({"label": f"Pente a = {a} NÉGATIVE — AMS hors service ou relation inverse", "severity": "critical", "ref": "XP X43-132 §7.3"})
            score -= 3.0
        elif a < NORM_CRITERIA["a_min"]:
            issues.append({"label": f"Pente a = {a} < 0,9 : sous-estimation systématique de l'AMS", "severity": "warn", "ref": "XP X43-132 recommandation"})
            score -= 0.5
        elif a > NORM_CRITERIA["a_max"]:
            issues.append({"label": f"Pente a = {a} > 1,2 : surestimation systématique de l'AMS", "severity": "warn", "ref": "XP X43-132 recommandation"})
            score -= 0.3

    # Intercept b vs VLE
    if b is not None and vle is not None and vle > 0:
        b_limit = NORM_CRITERIA["b_vle_pct"] * vle
        if abs(b) <= b_limit:
            positive.append({"label": f"Intercept b = {b} ≤ 10%·VLE = {b_limit:.2f} ✓", "ref": "XP X43-132 recommandation"})
        else:
            issues.append({"label": f"Intercept b = {b} > 10%·VLE = {b_limit:.2f}", "severity": "warn", "ref": "XP X43-132 recommandation"})
            score -= 0.3

    # Variabilité Sd
    if sd is not None and kv is not None:
        if kv == 0.0:
            issues.append({"label": f"Seuil 1,5σKv = 0,0 — anomalie de calcul dans le rapport", "severity": "error", "ref": "NF EN 14181 §5.4.3"})
            score -= 1.0
        elif sd <= kv:
            positive.append({"label": f"Variabilité Sd = {sd} ≤ seuil {kv} ✓", "ref": "NF EN 14181 §5.4.3"})
        else:
            issues.append({"label": f"Variabilité Sd = {sd} > seuil {kv} (non conforme)", "severity": "error", "ref": "NF EN 14181 §5.4.3"})
            score -= 2.0

    # Essais retirés
    if essais_retires > NORM_CRITERIA["max_essais_retires"]:
        issues.append({"label": f"{essais_retires} essais retirés > recommandation de 2 (XP X43-132)", "severity": "warn", "ref": "XP X43-132 §7.3"})
        score -= 0.3

    score = max(1.0, min(5.0, score))
    score = round(score * 2) / 2

    # Verdict final
    if verdict_override is not None:
        is_valid = verdict_override
    else:
        is_valid = score >= 3.5 and not any(i["severity"] == "critical" for i in issues)
        is_valid = is_valid and (sd is None or kv is None or kv == 0.0 or sd <= kv)

    result.update({
        "score": score,
        "score_stars": _score_to_stars(score),
        "score_color": _score_color(score),
        "valid": is_valid,
        "status_label": "Valide" if is_valid else "Invalide",
        "regulated": regulated,
        "issues": issues,
        "positive": positive,
        "nb_issues": len(issues),
    })
    return result


# ─── Score AMS global ─────────────────────────────────────────────────────────

def _compute_ams_score(channels: list, regulated_count: int, regulated_valid: int) -> float:
    if regulated_count == 0:
        return 3.0
    ratio = regulated_valid / regulated_count
    # Pondération: ratio de conformité + pénalité si problèmes critiques
    critical = sum(1 for c in channels for i in c.get("issues", []) if i.get("severity") == "critical")
    score = ratio * 5.0 - (critical * 0.5)
    return max(1.0, min(5.0, round(score * 2) / 2))


# ─── Plan d'action ────────────────────────────────────────────────────────────

def _generate_actions(rapport: dict, channels: list, meta: dict) -> list:
    actions = []
    n = 1

    def add(action, pollutant, target, cat, priority, delay, ref):
        nonlocal n
        actions.append({
            "num": n, "action": action, "pollutant": pollutant,
            "target": target, "category": cat, "priority": priority,
            "delay": delay, "ref": ref
        })
        n += 1

    # Actions pour chaque canal non conforme
    for ch in channels:
        name = (ch.get("name") or "?").upper()
        for issue in ch.get("issues", []):
            if issue["severity"] == "critical":
                add(
                    f"CRITIQUE — {name} : {issue['label']}. Arrêter l'utilisation réglementaire et réaliser un diagnostic complet de l'AMS. Nouveau QAL2 obligatoire.",
                    name, "Exploitant + Fournisseur AMS", "AMS", "P1", "Immédiat", issue["ref"]
                )
            elif issue["severity"] == "error" and not ch.get("valid"):
                add(
                    f"{name} non conforme — {issue['label']}. L'AMS ne peut pas assurer la surveillance réglementaire.",
                    name, "Exploitant + Fournisseur AMS", "AMS", "P1", "< 1 mois", issue["ref"]
                )
            elif issue["severity"] == "warn":
                add(
                    f"{name} — {issue['label']}. Vérifier et corriger avant le prochain AST.",
                    name, "Exploitant", "AMS", "P2", "< 3 mois", issue["ref"]
                )

    # Actions rapport labo
    for crit in rapport.get("criteres", []):
        if crit["status"] == "ko":
            add(
                f"Rapport labo — {crit['label']}. Contacter le laboratoire pour correction.",
                "Rapport", "Laboratoire", "LABO", "P1", "< 2 semaines", crit["ref"]
            )
        elif crit["status"] == "partial":
            add(
                f"Rapport labo — {crit['label']}. Vérifier avec le laboratoire.",
                "Rapport", "Laboratoire", "LABO", "P2", "< 1 mois", crit["ref"]
            )

    # Actions exploitation standard
    nb_valid = sum(1 for ch in channels if ch.get("valid"))
    if nb_valid > 0:
        add(
            "Mettre en place la surveillance hebdomadaire du domaine d'étalonnage valide pour les canaux conformes. Documenter les dépassements et déclencher un nouveau QAL2 si critères seuils atteints.",
            "Canaux conformes", "Exploitant", "EXPLOIT", "P2", "Continu", "XP X43-132 §9"
        )
        add(
            "Mettre à jour les cartes de contrôle QAL3 (Shewhart) avec les nouvelles fonctions d'étalonnage. Transmettre à l'opérateur QAL3.",
            "Canaux conformes", "Exploitant", "DOCS", "P2", "< 2 mois", "NF EN 14181 §6"
        )

    add(
        "Transmettre le rapport QAL2 à l'inspection DREAL/DRIEAT et mettre à jour le dossier réglementaire avec les mesures correctives prévues.",
        "Tous", "Exploitant", "DOCS", "P2", "< 1 mois", "Arrêté préfectoral + AM 20/09/2002"
    )
    add(
        "Planifier le prochain AST (canaux valides) et les QAL2 de remise en conformité (canaux invalides).",
        "Tous", "Exploitant + Labo", "EXPLOIT", "P3", "< 12 mois", "NF EN 14181 §7"
    )

    return actions


# ─── Vérification COFRAC ──────────────────────────────────────────────────────

def check_cofrac_status(cofrac_number: Optional[str] = None, labo_name: Optional[str] = None) -> dict:
    """
    Vérifie le statut d'accréditation COFRAC.
    Simple vérification de disponibilité du site + lien direct.
    """
    import requests
    base_url = "https://www.cofrac.fr"
    result = {
        "status": "unknown",
        "cofrac_number": cofrac_number,
        "labo": labo_name,
        "url": None,
        "message": "",
        "checked_at": None,
    }
    from datetime import datetime
    result["checked_at"] = datetime.now().isoformat()

    try:
        # Vérification que le site COFRAC est accessible
        r = requests.get(f"{base_url}/accreditations/les-organismes-accredites/", timeout=8)
        if r.status_code == 200:
            result["status"] = "site_ok"
            result["message"] = "Site COFRAC accessible. Vérification manuelle recommandée."
            if cofrac_number:
                result["url"] = f"{base_url}/accreditations/les-organismes-accredites/?numero={cofrac_number}"
                result["message"] = f"Lien direct vers l'accréditation n°{cofrac_number} disponible. Vérification manuelle nécessaire."
        else:
            result["status"] = "site_error"
            result["message"] = f"Site COFRAC non accessible (HTTP {r.status_code})"
    except Exception as e:
        result["status"] = "error"
        result["message"] = f"Impossible de joindre cofrac.fr : {str(e)}"

    return result


# ─── Vérification normes ──────────────────────────────────────────────────────

def check_norm_updates() -> list:
    """Retourne les informations sur les versions normatives en vigueur."""
    updates = []
    for norm, info in NORM_VERSIONS.items():
        updates.append({
            "norm": norm,
            "current_version": info["current"],
            "reference": info["ref"],
            "status": "current",
            "message": f"Version en vigueur : {info['ref']}",
            "source": "Base de connaissance ENVEA (mise à jour manuelle recommandée)"
        })
    return updates


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _score_to_stars(score: float) -> str:
    full = int(score)
    half = 1 if (score - full) >= 0.5 else 0
    empty = 5 - full - half
    return "★" * full + ("·" if half else "") + "☆" * empty


def _score_color(score: float) -> str:
    if score >= 4.0: return "success"
    if score >= 3.0: return "warning"
    return "danger"


def _score_css(score: float) -> str:
    if score >= 4.0: return "#27AE60"
    if score >= 3.0: return "#E67E22"
    return "#C0392B"
