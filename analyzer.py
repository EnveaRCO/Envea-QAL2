"""
analyzer.py — Moteur d'analyse normatif QAL2 complet
Références : NF EN 14181, XP X43-132, NF EN ISO/IEC 17025
Objectif : défendre les appareils ENVEA face aux conclusions d'un laboratoire.
"""
import re
import math
from typing import Optional

# ─── Critères normatifs ───────────────────────────────────────────────────────

NORM_CRITERIA = {
    "r2_gas_min":       0.9,    # R² minimum gaz(recommandation XP X43-132)
    "r2_dust_min":       0.8,    # R² minimum poussières/opacimètre
    "a_min":             0.9,    # Pente minimum
    "a_max":             1.2,    # Pente maximum
    "b_vle_pct":         0.10,   # Intercept ≤ 10% VLE
    "max_essais_retires":2,      # Max essais retirés (recommandation XP X43-132 §7.3)
    "min_essais_valides":15,     # NF EN 14181 §5.2.1
    "min_jours":         3,      # NF EN 14181 ��5.2.1
    "tps_reponse_max":   200,    # secondes (gaz!�tandards) — XP X43-132 §5.4
    "tps_reponse_hcl_hf":400,   # secondes (HCl, HF, NH3, Hg)
}

NORM_VERSIONS = {
    "NF EN 14181":      {"current": "2014", "ref": "NF EN 14181:2014"},
    "XP X43-132":       {"current": "2015", "ref": "XP X43-132:2015"},
    "NF EN 15259":      {"current": "2007", "ref": "NF EN 15259:2007"},
    "NF EN ISO/IEC 17025": {"current": "2017", "ref": "NF EN ISO/IEC 17025:2017"},
}

DUST_POLLUTANTS   = {"poussieres", "poussières", "dust"}
HCL_HF_SLOW_RESP  = {"hcl", "hf", "nh3", "hm"}
REGULATED_DEFAULT = {"co", "nox", "covt", "poussieres", "so2", "hcl", "hf"}


# ─── Analyse principale —───────────────────────────────────────────────────────

def analyze(parsed_data: dict) -> dict:
    """Analyse complète d'un rapport QAL2 parsé."""
    meta     = parsed_data.get("meta", {})
    pollutants = parsed_data.get("pollutants", [])
    raw_text = parsed_data.get("raw_text", "")
    ops      = parsed_data.get("operational_tests", {})
    resp_times = parsed_data.get("response_times", [])
    derogations = parsed_data.get("derogations", [])

    # Analyse par canal
    ams_results = [_analyze_channel(p, raw_text) for p in pollutants]

    valid_count    = sum(1 for p in ams_results if p.get("valid") is True)
    invalid_count  = sum(1 for p in ams_results if p.get("valid") is False)
    regulated_count= sum(1 for p in ams_results if p.get("regulated"))
    regulated_valid= sum(1 for p in ams_results if p.get("valid") and p.get("regulated"))

    # Analyse rapport global
    rapport = _analyze_rapport(meta, pollutants, raw_text, ops, resp_times, derogations)

    # Score AMS
    score_ams = _compute_ams_score(ams_results, regulated_count, regulated_valid)

    # Points de défense ENVEA
    defense_points = _generate_defense_points(ams_results, ops, resp_times, meta, derogations, raw_text)

    # Plan d'action
    actions = _generate_actions(rapport, ams_results, meta, ops, resp_times)

    return {
        "meta":              meta,
        "rapport":           rapport,
        "ams_channels":      ams_results,
        "operational_tests": ops,
        "response_times":    resp_times,
        "derogations":       derogations,
        "defense_points":    defense_points,
        "score_rapport":     rapport["score"],
        "score_ams":         score_ams,
        "nb_valid":          valid_count,
        "nb_invalid":        invalid_count,
        "nb_regulated":      regulated_count,
        "nb_regulated_valid":regulated_valid,
        "actions":           actions,
        "nb_actions":        len(actions),
        "nb_urgent":         ` sum(1 for a in actions if a["priority"] == "P1"),
        "nb_defense":        len(defense_points),
    }


# ─── Analyse rapport global ────────────────────────────────────────────────────

def _analyze_rapport(meta, pollutants, raw_text, ops, resp_times, derogations) -> dict:
    criteres = []
    deductions = 0.0

    def add(label, status, points, ref, detail=""):
        nonlocal deductions
        criteres.append({"label": label, "status": status, "points": points, "ref": ref, "detail": detail})
        if status in ("ko", "partial"):
            deductions += points

    # ── 1. Accréditation COFRAC ──
    if meta.get("labo_cofrac"):
        add(f"Accréditation COFRAC n°{meta['labo_cofrac']} présente", "ok", 0, "NF EN ISO/IEC 17025 §4.1")
    elif re.search(r'cofrac|accr[eé]ditation\s+essai', raw_text[:3000], re.IGNORECASE):
        add("Mention COFRAC présente mais n° non extrait", "partial", 0.2, "NF EN ISO/IEC 17025 §4.1")
    else:
        add("Accréditation COFRAC non mentionnée dans le rapport", "ko", 1.5, "NF EN ISO/IEC 17025 §4.1")

    # ── 2. Nombre d'essais valides minimum ──
    # Vérifier par canal
    under_15 = [p for p in pollutants if p.get("nb_essais_valides") is not None and p.get("nb_essais_valides") < 15]
    if under_15:
        names = ", ".join(p.get("name","?").upper() for p in under_15)
        add(f"Essais valides < 15 pour : {names}", "ko", 1.5, "NF EN 14181 §5.2.1")
    elif meta.get("nb_essais") and meta["nb_essais"] >= 15:
        add(f"{meta['nb_essais']} essais SRM réalisés (≥ 15 requis)", "ok", 0, "NF EN 14181 §5.2.1")
    else:
        at15 = [p for p in pollutants if p.get("nb_essais_valides", 0) >= 15]
        if at15:
            add(f"{len(at15)} canaux avec ≥ 15 essais valides", "ok", 0, "NF EN 14181 §5.2.1")
        else:
            add("Nombre d'essais valides non vérifié (extraction incomplète)", "partial", 0.3, "NF EN 14181 §5.2.1")

    # ── 3. Essais retirés ──
    over_retires = [p for p in pollutants if (p.get("essais_retires") or 0) > NORM_CRITERIA["max_essais_retires"]]
    if over_retires:
        names = ", ".join(f"{p.get('name','?').upper()} ({p.get('essais_retires')} retirés)" for p in over_retires)
        add(f"Essais retirés > 2 pour : {names} — recommandation XP X43-132 §7.3",
            "partial", 0.5, "XP X43-132 §7.3",
            "La norme recommande ≤ 2 essais retirés. Un dépassement nécessite une justification écrite du laboratoire.")
    else:
        add("Essais retirés ≤ 2 pour tous les canaux", "ok", 0, "XP X43-132 §7.3")

    # ── 4. Tests opérationnels ──
    if ops:
        ops_issues = []
        if ops.get("etancheite") and not ops["etancheite"].get("conforme"):
            ops_issues.append("Test d'étanchéité NON CONFORME")
        if ops.get("mr_non_extractif") and not ops["mr_non_extractif"].get("realise"):
            ops_issues.append("Injections MR AMS non-extractifs non réalisées (§9.8)")
        if ops.get("documentation", {}).get("nb_manquant", 0) > 3:
            n = ops["documentation"]["nb_manquant"]
            ops_issues.append(f"{n} documents AMS manquants (§9.3)")
        if ops_issues:
            add("Tests opérationnels — anomalies : " + " | ".join(ops_issues),
                "partial", 0.5, "NF EN 14181 §5.3",
                "Les manquements documentaires sont à la charge de l'exploitant/opérateur, pas du fournisseur AMS.")
        else:
            add("Tests opérationnels documentés et conformes", "ok", 0, "NF EN 14181 §5.3")
    else:
        if re.search(r'test\s+opér|alignement|étanchéité|aptitude', raw_text, re.IGNORECASE):
            add("Tests opérationnels présents (extraction partielle)", "partial", 0.2, "NF EN 14181 §5.3")
        else:
            add("Tests opérationnels non trouvés dans le rapport", "partial", 0.5, "NF EN 14181 §5.3")

    # ── 5. Temps de réponse ──
    nc_times = [t for t in resp_times if t.get("non_conforme")]
    direct_inj = [t for t in nc_times if t.get("direct_injection")]
    if nc_times:
        names = ", ".join(t["param"].upper() for t in nc_times)
        detail = ""
        if direct_inj:
            detail = (f"IMPORTANT : pour {', '.join(t['param'].upper() for t in direct_inj)}, "
                      "l'injection a été réalisée en direct analyseur et non en bout de sonde. "
                      "Le temps de réponse mesuré n'inclut pas la ligne de prélèvement chauffée. "
                      "Ce test n'est donc pas représentatif des conditions réelles d'exploitation.")
        add(f"Temps de réponse NON CONFORMES : {names}",
            "ko", 0.8, "XP X43-132 §5.4 + NF EN 14181 §5.3.5", detail)
    elif resp_times:
        add("Temps de réponse conformes pour tous les paramètres", "ok", 0, "XP X43-132 §5.4")
    else:
        tps_nc = re.search(r'temps.{0,20}r[eé]ponse.{0,100}non\s+conforme', raw_text, re.IGNORECASE | re.DOTALL)
        if tps_nc:
            add("Temps de réponse non conformes détectés dans le rapport", "ko", 0.8, "XP X43-132 §5.4")
        else:
            add("Temps de réponse (extraction non disponible — vérifier manuellement)", "partial", 0.1, "XP X43-132 §5.4")

    # ── 6. Procédure/Cas justifiés ──
    cas_found = [p for p in pollutants if p.get("cas")]
    if cas_found:
        cas_list = ", ".join(f"{p.get('name','?').upper()}=Cas {p.get('cas')}" for p in cas_found)
        add(f"Procédures d'étalonnage explicites : {cas_list}", "ok", 0, "XP X43-132 §7.2")
    elif re.search(r'cas\s+[AB]\s+(?:selon|à appliquer)', raw_text, re.IGNORECASE):
        add("Procédure Cas A/B mentionnée mais non extraite systématiquement", "partial", 0.1, "XP X43-132 §7.2")
    else:
        add("Procédure d'étalonnage (Cas A/B/C) non explicitement justifiée", "partial", 0.3, "XP X43-132 §7.2")

    # ── 7. Dérogations ──
    if derogations:
        n = len(derogations)
        add(f"{n} dérogation(s) à la norme NF EN 14181 détectée(s)",
            "partial", 0.3 * min(n, 2), "NF EN 14181 §5.4 / XP X43-132 §7",
            "Chaque dérogation doit être justifiée et acceptée par l'autorité compétente.")
    else:
        # Chercher les dérogations typiques
        if re.search(r'déroga\w+|VLE.*remplacée|moins de 5 couples', raw_text, re.IGNORECASE):
            add("Dérogation(s) appliquée(s) (non extraites automatiquement)", "partial", 0.2, "NF EN 14181")

    # ── 8. Documentation AMS ──
    doc_ops = ops.get("documentation", {})
    if doc_ops.get("present"):
        nb_non = doc_ops.get("nb_manquant", 0)
        docs_items = doc_ops.get("items", {})
        critiques = []
        if docs_items.get("fiche_suivi") == "NON":
            critiques.append("Fiche de suivi AMS")
        if docs_items.get("qal3") == "NON":
            critiques.append("Documentation QAL3")
        if docs_items.get("procedures_maintenance") == "NON":
            critiques.append("Procédures de maintenance")
        if docs_items.get("formation") == "NON":
            critiques.append("Enregistrements formation")
        if critiques:
            add(f"Documents AMS manquants : {', '.join(critiques)}",
                "partial", 0.3, "NF EN 14181 §5.3.2",
                "Ces manquements documentaires incombent à l'exploitant. "
                "ENVEA peut fournir les rapports de maintenance (Portail GLPI).")
    elif re.search(r'documentation.{0,30}(OUI|NON)', raw_text, re.IGNORECASE):
        add("Documentation AMS vérifiée (extraction partielle)", "partial", 0.1, "NF EN 14181 §5.3.2")

    # ── 9. Aptitude à l'emploi ──
    apt_ops = ops.get("aptitude", {})
    if apt_ops.get("present"):
        apt_non = [i for i in apt_ops.get("items", []) if i.get("status") == "NON"]
        if apt_non:
            items_label = "; ".join(i["label"][:40] for i in apt_non[:3])
            add(f"Aptitude à l'emploi — points NON : {items_label}",
                "partial", 0.2, "NF EN 14181 §5.3.3",
                "Certains points NON (ex: shelter extérieur) sont des contraintes de site hors contrôle ENVEA.")

    score = max(1.0, 5.0 - deductions)
    score = round(score * 2) / 2

    return {
        "score":       score,
        "score_stars": _score_to_stars(score),
        "score_color": _score_color(score),
        "criteres":    criteres,
        "anomalies":   sum(1 for c in criteres if c["status"] in ("ko", "partial")),
        "errors":      sum(1 for c in criteres if c["status"] == "ko"),
    }


# ─── Analyse canal AMS ────────────────────────────────────────────────────────

def _analyze_channel(p: dict, raw_text: str = "") -> dict:
    name = p.get("name", "?")
    result = dict(p)

    # Garantir toutes les clés attendues par le template
    for k in ('kv_threshold','sd','a','b','r2','vle','strategy','cas','name_raw',
              'valid','essais_retires','nb_essais_valides','sigma0','kv','derogation',
              'domaine_min','domaine_max','unit'):
        result.setdefault(k, None)
    result.setdefault('essais_retires', 0)
    result.setdefault('name_raw', result.get('name', ''))

    issues   = []
    positive = []

    is_dust  = name in DUST_POLLUTANTS or "poussiere" in name.lower()
    r2_min   = NORM_CRITERIA["r2_dust_min"] if is_dust else NORM_CRITERIA["r2_gas_min"]
    regulated = name in REGULATED_DEFAULT
    result["regulated"] = regulated

    a    = p.get("a")
    b    = p.get("b")
    r2   = p.get("r2")
    sd   = p.get("sd")
    kv   = p.get("kv_threshold")
    vle  = p.get("vle")
    essais_retires = p.get("essais_retires", 0) or 0
    nb_valides = p.get("nb_essais_valides")
    verdict_from_report = p.get("valid")

    score = 5.0

    # ── R² ──
    if r2 is not None:
        if r2 >= r2_min:
            positive.append({
                "label": f"R² = {r2:.4f} ≥ {r2_min} ✓",
                "ref": "XP X43-132 recommandation"
            })
        elif r2 >= r2_min - 0.05:
            issues.append({
                "label": f"R² = {r2:.4f} légèrement inférieur au seuil recommandé {r2_min}",
                "severity": "warn",
                "ref": "XP X43-132 recommandation",
                "defense": "R² marginalement hors seuil. La recommandation XP X43-132 n'est pas une exigence normative opposable."
            })
            score -= 0.5
        elif r2 >= 0.5:
            issues.append({
                "label": f"R² = {r2:.4f} insuffisant (seuil recommandé ≥ {r2_min}) — corrélation AMS/SRM faible",
                "severity": "error",
                "ref": "XP X43-132 recommandation",
                "defense": "Faible R² peut indiquer une variabilité élevée des mesures SRM plutôt qu'un défaut AMS. Analyser la dispersion SRM."
            })
            score -= 1.5
        else:
            issues.append({
                "label": f"R² = {r2:.4f} — corrélation AMS/SRM quasi nulle (< 0.5). Fonction d'étalonnage invalide.",
                "severity": "critical",
                "ref": "XP X43-132 recommandation",
                "defense": "Un R² < 0,5 indique que l'AMS et la SRM ne mesurent pas le même signal dans les mêmes conditions. Vérifier la représentativité des mesures SRM et la synchronisation temporelle."
            })
            score -= 2.5

    # ── Pente a ──
    if a is not None:
        if a < 0:
            issues.append({
                "label": f"Pente a = {a} NÉGATIVE — relation inverse AMS/SRM : l'AMS ne mesure pas correctement ce polluant",
                "severity": "critical",
                "ref": "XP X43-132 §7.3",
                "defense": "Pente négative impossible physiquement. Cela indique soit une erreur de synchronisation des mesures, soit que l'AMS est saturé/non-fonctionnel pour ce composé dans les conditions mesurées. Demander la vérification de la synchronisation temporelle AMS-SRM."
            })
            score -= 3.0
        elif a < NORM_CRITERIA["a_min"]:
            issues.append({
                "label": f"Pente a = {a:.3f} < 0,9 — sous-estimation systématique de l'AMS",
                "severity": "warn",
                "ref": "XP X43-132 recommandation",
                "defense": f"Pente de {a:.3f} indique que l'AMS sous-estime légèrement la SRM. La plage recommandée [0,9–1,2] est une recommandation XP X43-132, non une exigence NF EN 14181. Une fonction d'étalonnage y = {a}·x + {b if b else '?'} corrige cette dérive."
            })
            score -= 0.5
        elif a > NORM_CRITERIA["a_max"]:
            issues.append({
                "label": f"Pente a = {a:.3f} > 1,2 — surestimation systématique de l'AMS",
                "severity": "warn",
                "ref": "XP X43-132 recommandation",
                "defense": f"Pente de {a:.3f} hors de la plage recommandée [0,9–1,2]. C'est une recommandation XP X43-132, non une exigence normative opposable. La fonction y = {a}·x + {b if b else '?'} recalibre l'AMS."
            })
            score -= 0.3
        else:
            positive.append({
                "label": f"Pente a = {a:.3f} ∈ [0,9 ; 1,2] ✓",
                "ref": "XP X43-132 recommandation"
            })

    # ── Intercept b vs VLE ──
    if b is not None and vle is not None and vle > 0:
        b_limit = NORM_CRITERIA["b_vle_pct"] * vle
        if abs(b) <= b_limit:
            positive.append({
                "label": f"Intercept b = {b:.3f} ≤ 10%·VLE = {b_limit:.2f} ✓",
                "ref": "XP X43-132 recommandation"
            })
        else:
            issues.append({
                "label": f"Intercept b = {b:.3f} > 10%·VLE = {b_limit:.2f}",
                "severity": "warn",
                "ref": "XP X43-132 recommandation",
                "defense": "Intercept hors de la recommandation 10%·VLE. La fonction d'étalonnage intègre cet offset et le compense automatiquement dans les calculs de conformité."
            })
            score -= 0.3
    elif b is not None:
        if b == 0.0:
            positive.append({"label": "Intercept b = 0 (cas B — droite forcée à l'origine) ✓", "ref": "XP X43-132 §7.2"})

    # ── Test de variabilité Sd ≤ σ0·Kv ──
    if sd is not None and kv is not None:
        if kv == 0.0:
            issues.append({
                "label": "Seuil σ0·Kv = 0,0 — anomalie de calcul dans le rapport du laboratoire",
                "severity": "error",
                "ref": "NF EN 14181 §5.4.3",
                "defense": "Un seuil de 0,0 est physiquement impossible (Kv > 0 toujours). Cela indique une erreur de calcul dans le rapport du laboratoire. Demander la correction du rapport."
            })
            score -= 1.0
        elif sd <= kv:
            positive.append({
                "label": f"Variabilité Sd = {sd:.2f} ≤ σ0·Kv = {kv:.2f} — test de variabilité réussi ✓",
                "ref": "NF EN 14181 §5.4.3"
            })
        else:
            margin = sd - kv
            pct = (sd / kv - 1) * 100 if kv > 0 else 999
            issues.append({
                "label": f"Test variabilité ÉCHOUÉ : Sd = {sd:.2f} > σ0·Kv = {kv:.2f} (dépassement +{pct:.0f}%)",
                "severity": "error",
                "ref": "NF EN 14181 §5.4.3",
                "defense": (
                    f"Sd = {sd:.2f} dépasse le seuil σ0·Kv = {kv:.2f}. "
                    "Analyser si ce dépassement est lié à une dispersion des mesures SRM (variabilité du procédé) "
                    "ou à un biais systématique de l'AMS. "
                    "Si la variabilité SRM est due aux conditions opératoires (fluctuations process), "
                    "c'est une contrainte externe à l'AMS. "
                    "Vérifier également si la dérogation VLE→concentration max SRM a été correctement appliquée."
                )
            })
            score -= 2.0
    elif sd is not None:
        # Recalculer si sigma0 et kv disponibles
        sigma0 = p.get("sigma0")
        kv_val = p.get("kv")
        vle_val = p.get("vle")
        if sigma0 and kv_val and vle_val:
            threshold = sigma0 / 100 * kv_val * vle_val / 1.96
            result["kv_threshold"] = round(threshold, 2)
            if sd <= threshold:
                positive.append({
                    "label": f"Variabilité Sd = {sd:.2f} ≤ σ0·Kv recalculé = {threshold:.2f} ✓",
                    "ref": "NF EN 14181 §5.4.3"
                })
            else:
                issues.append({
                    "label": f"Variabilité Sd = {sd:.2f} > σ0·Kv recalculé = {threshold:.2f}",
                    "severity": "error",
                    "ref": "NF EN 14181 §5.4.3",
                    "defense": "Vérifier les données de calcul σ0, Kv et VLE utilisées par le laboratoire."
                })
                score -= 2.0

    # ── Essais retirés ──
    if essais_retires > NORM_CRITERIA["max_essais_retires"]:
        issues.append({
            "label": f"{essais_retires} essais retirés > recommandation de 2 (XP X43-132 §7.3)",
            "severity": "warn",
            "ref": "XP X43-132 §7.3",
            "defense": (
                f"La règle des ≤ 2 essais retirés est une recommandation XP X43-132, "
                "non une exigence NF EN 14181. Un dépassement peut être accepté si le laboratoire "
                "justifie chaque retrait (conditions opératoires anormales, dysfonctionnement SRM, etc.). "
                "Demander la justification écrite de chaque essai retiré."
            )
        })
        score -= 0.3

    # ── Nombre essais valides ──
    if nb_valides is not None and nb_valides < 15:
        issues.append({
            "label": f"Seulement {nb_valides} essais valides (minimum 15 requis par NF EN 14181 §5.2.1)",
            "severity": "error",
            "ref": "NF EN 14181 §5.2.1",
            "defense": "Moins de 15 essais valides invalide la campagne QAL2 pour ce canal selon NF EN 14181. Ce canal ne peut pas être déclaré conforme."
        })
        score -= 1.5
    elif nb_valides is not None and nb_valides >= 15:
        positive.append({
            "label": f"{nb_valides} essais valides (≥ 15 requis) ✓",
            "ref": "NF EN 14181 §5.2.1"
        })

    # ── Cas/Stratégie ──
    cas = p.get("cas")
    if cas == "B":
        positive.append({
            "label": "Cas B (droite forcée à zéro) — applicable quand la plage de mesure est < 15% VLE",
            "ref": "XP X43-132 §7.2"
        })

    # ── Dérogation canal ──
    if p.get("derogation"):
        issues.append({
            "label": f"Dérogation appliquée : {p['derogation'][:120]}",
            "severity": "warn",
            "ref": "NF EN 14181 §5.4",
            "defense": "La dérogation (VLE remplacée par concentration max SRM) est utilisée quand les émissions sont trop faibles pour réaliser un test de variabilité standard. Cette approche reste dans l'esprit de la norme mais doit être justifiée."
        })
        score -= 0.2

    score = max(1.0, min(5.0, score))
    score = round(score * 2) / 2

    # Verdict final
    if verdict_from_report is not None:
        is_valid = verdict_from_report
    else:
        has_critical = any(i["severity"] == "critical" for i in issues)
        has_sd_fail  = any("variabilité" in i["label"].lower() and "échoué" in i["label"].lower() for i in issues)
        is_valid = not has_critical and not has_sd_fail and score >= 3.0

    result.update({
        "score":        score,
        "score_stars":  _score_to_stars(score),
        "score_color":  _score_color(score),
        "valid":        is_valid,
        "status_label": "Valide" if is_valid else "Invalide",
        "regulated":    regulated,
        "issues":       issues,
        "positive":     positive,
        "nb_issues":    len(issues),
        "nb_positive":  len(positive),
    })
    return result


# ─── Points de défense ENVEA ──────────────────────────────────────────────────

def _generate_defense_points(channels, ops, resp_times, meta, derogations, raw_text) -> list:
    """
    Génère les arguments de défense pour ENVEA face aux conclusions du laboratoire.
    Chaque point identifie une faiblesse dans la méthodologie du labo ou
    propose un contre-argument technique.
    """
    points = []
    n = [0]

    def add(category, severity, title, argument, ref="", action=""):
        n[0] += 1
        points.append({
            "num": n[0],
            "category": category,
            "severity": severity,  # "critique" / "important" / "info"
            "title": title,
            "argument": argument,
            "ref": ref,
            "action": action,
        })

    # ── Temps de réponse injections directes ──
    direct_nc = [t for t in resp_times if t.get("non_conforme") and t.get("direct_injection")]
    if direct_nc:
        params = ", ".join(t["param"].upper() for t in direct_nc)
        add(
            "Temps de réponse",
            "critique",
            f"Test TR {params} réalisé en injection directe analyseur — non représentatif",
            (
                f"Le temps de réponse de {params} a été mesuré par injection directe à l'entrée de l'analyseur, "
                "en court-circuitant la ligne de prélèvement chauffée. "
                "La norme XP X43-132 §5.4 impose une mesure incluant le trajet complet (sonde → ligne → analyseur). "
                "Ce test ne valide donc pas le temps de réponse réel de l'AMS en conditions d'exploitation. "
                "La non-conformité déclarée est donc elle-même non conforme à la procédure normative."
            ),
            "XP X43-132 §5.4 + NF EN 14181 §5.3.5",
            "Exiger du laboratoire qu'il refasse le test TR avec injection en bout de sonde, ou qu'il justifie l'équivalence."
        )

    # ── Temps de réponse non conformes (sans injection directe) ──
    other_nc_tr = [t for t in resp_times if t.get("non_conforme") and not t.get("direct_injection")]
    if other_nc_tr:
        for t in other_nc_tr:
            p = t["param"].upper()
            ts = t.get("time_s")
            lim = t.get("limit_s", 200)
            add(
                "Temps de réponse",
                "important",
                f"Temps de réponse {p} non conforme ({ts}s mesuré, limite {lim}s)",
                (
                    f"Le TR de {p} ({ts}s) dépasse la limite de {lim}s. "
                    "Vérifier si la ligne de prélèvement a été purgée correctement avant le test. "
                    "Un temps de réponse élevé peut être lié à un volume mort dans la ligne (pas un défaut AMS). "
                    "La conformité TR doit être comparée à la valeur QAL1 (pas seulement à la limite générique)."
                ),
                "XP X43-132 §5.4",
                f"Demander la valeur TR mesurée lors du QAL1 pour {p} et comparer."
            )

    # ── Canaux avec R² très bas ──
    for ch in channels:
        r2 = ch.get("r2")
        name = ch.get("name", "?").upper()
        if r2 is not None and r2 < 0.5:
            add(
                "Corrélation AMS/SRM",
                "critique",
                f"R²({name}) = {r2:.4f} — corrélation quasi nulle",
                (
                    f"Un R² de {r2:.4f} signifie que la SRM n'explique que {r2*100:.1f}% de la variance AMS. "
                    "Cela peut résulter de : (1) mesures SRM non synchronisées temporellement avec l'AMS, "
                    "(2) représentativité de la SRM dans la section de mesure, "
                    "(3) émissions très faibles proches des limites de détection SRM. "
                    "Une R² aussi basse invalide la pertinence de la fonction d'étalonnage elle-même."
                ),
                "XP X43-132 §7.3",
                f"Demander les chronogrammes synchronisés AMS/{name} SRM pour vérifier la temporalité."
            )
        elif r2 is not None and r2 < 0.7:
            add(
                "Corrélation AMS/SRM",
                "important",
                f"R²({name}) = {r2:.4f} — corrélation faible",
                (
                    f"R² de {r2:.4f} inférieur au seuil recommandé {0.9 if name not in DUST_POLLUTANTS else 0.8}. "
                    "La dispersion SRM peut être due à la variabilité du procédé (pas un défaut AMS). "
                    "Vérifier si les mesures SRM sont réalisées en conditions représentatives du fonctionnement normal."
                ),
                "XP X43-132 recommandation",
                f"Analyser la variabilité process pendant les mesures {name}."
            )

    # ── Pente négative ──
    for ch in channels:
        a = ch.get("a")
        if a is not None and a < 0:
            name = ch.get("name", "?").upper()
            add(
                "Fonction d'étalonnage",
                "critique",
                f"Pente a({name}) = {a:.3f} — NÉGATIVE : physiquement impossible",
                (
                    f"Une pente négative pour {name} est physiquement impossible. "
                    "Elle indique que les valeurs AMS et SRM évoluent en sens inverse, ce qui ne peut être dû qu'à : "
                    "(1) une erreur de synchronisation temporelle (décalage AMS/SRM non corrigé), "
                    "(2) une unité ou conversion différente entre AMS et SRM, "
                    "(3) une erreur dans la numérotation des essais. "
                    "Cette fonction d'étalonnage doit être déclarée invalide par le laboratoire lui-même."
                ),
                "NF EN 14181 §5.4.2 + XP X43-132 §7.3",
                f"Contester formellement le résultat {name} et demander une vérification des données brutes."
            )

    # ── Essais retirés > 2 ──
    over_ret = [p for p in channels if (p.get("essais_retires") or 0) > 2]
    if over_ret:
        for ch in over_ret:
            name = ch.get("name", "?").upper()
            n_ret = ch.get("essais_retires", 0)
            add(
                "Essais retirés",
                "important",
                f"{n_ret} essais retirés pour {name} — recommandation ≤ 2",
                (
                    f"{n_ret} essais ont été retirés pour {name}. "
                    "La règle des ≤ 2 essais retirés est une RECOMMANDATION XP X43-132 §7.3, "
                    "pas une exigence normative de NF EN 14181. "
                    "Chaque retrait doit être justifié (conditions opératoires, défaillance SRM, etc.). "
                    "Sans justification écrite, ces retraits peuvent être contestés. "
                    "Avec justification valable, la règle peut être appliquée de manière dérogatoire."
                ),
                "XP X43-132 §7.3",
                f"Demander au labo la justification écrite de chaque essai retiré pour {name}."
            )

    # ── Dérogation variabilité (VLE → concentration max) ──
    for ch in channels:
        if ch.get("derogation") and "vle" in (ch.get("derogation") or "").lower():
            name = ch.get("name", "?").upper()
            add(
                "Dérogation normative",
                "important",
                f"Dérogation variabilité {name} : VLE remplacée par concentration max SRM",
                (
                    f"Pour {name}, la VLE a été remplacée par la concentration maximale des SRM retenues "
                    "car moins de 5 couples de données sont au-dessus de la VLE. "
                    "Cette dérogation est hors NF EN 14181 §5.4 mais admise par XP X43-132 dans certains cas. "
                    "Elle modifie le seuil σ0·Kv et peut artificiellement rendre le test conforme ou non conforme. "
                    "Vérifier que le σ0 utilisé correspond bien au polluant (pas une valeur générique)."
                ),
                "NF EN 14181 §5.4 / XP X43-132 §7.4",
                f"Vérifier le calcul détaillé σ0·Kv pour {name} avec la concentration max retenue."
            )

    # ── MR AMS non-extractifs non réalisées ──
    mr_ops = ops.get("mr_non_extractif", {})
    if mr_ops.get("present") and not mr_ops.get("realise"):
        add(
            "Tests opérationnels",
            "important",
            "Injections MR AMS non-extractifs (§9.8) non réalisées",
            (
                "Les injections de matériaux de référence sur l'AMS poussières (non-extractif) n'ont pas été "
                "réalisées lors de cette campagne. La norme NF EN 14181 §5.3 impose cette vérification. "
                "L'absence de ce contrôle fragilise la démonstration de conformité de l'AMS poussières. "
                "Cependant, si le constructeur a fourni les contrôles d'alignement et de propreté (§9.1), "
                "l'aptitude de base de l'AMS peut être considérée comme vérifiée."
            ),
            "NF EN 14181 §5.3.7",
            "Planifier les injections MR sur l'AMS poussières lors de la prochaine maintenance ENVEA."
        )

    # ── Documentation manquante ──
    doc_ops = ops.get("documentation", {})
    if doc_ops.get("present"):
        items = doc_ops.get("items", {})
        if items.get("fiche_suivi") == "NON":
            add(
                "Documentation",
                "info",
                "Fiche de suivi AMS absente",
                (
                    "La fiche de suivi (tout événement concernant l'AMS) est indiquée comme manquante. "
                    "ENVEA dispose du portail GLPI comme outil de traçabilité des interventions. "
                    "Les rapports de maintenance ENVEA transmis au client constituent un substitut recevable. "
                    "Ce point est à la responsabilité de l'exploitant, pas du fournisseur AMS."
                ),
                "NF EN 14181 §5.3.2",
                "Fournir les exports GLPI au laboratoire comme justificatif de suivi."
            )
        if items.get("qal3") == "NON":
            add(
                "Documentation",
                "important",
                "Documentation QAL3 absente — surveillance continue non documentée",
                (
                    "L'absence de documentation QAL3 est signalée. La norme NF EN 14181 §6 impose "
                    "la mise en place d'une surveillance QAL3 (cartes de contrôle Shewhart) après chaque QAL2. "
                    "Si le QAL3 n'a pas encore été mis en place suite à ce QAL2, c'est acceptable si "
                    "la mise en place est planifiée dans un délai raisonnable (mentionné dans le rapport)."
                ),
                "NF EN 14181 §6",
                "Mettre en place les cartes QAL3 avec les nouvelles fonctions d'étalonnage."
            )

    # ── Variabilité très stricte (σ0·Kv très faible) ──
    for ch in channels:
        sd = ch.get("sd")
        kv = ch.get("kv_threshold")
        name = ch.get("name", "?").upper()
        if sd is not None and kv is not None and kv > 0 and sd > kv:
            ratio = sd / kv
            if ratio > 5:
                add(
                    "Test de variabilité",
                    "important",
                    f"Seuil σ0·Kv({name}) très strict : Sd/{kv:.2f} = {ratio:.1f}×",
                    (
                        f"Le seuil σ0·Kv = {kv:.2f} est très faible pour {name}, "
                        f"entraînant un écart Sd/seuil de {ratio:.1f}×. "
                        "Vérifier le σ0 utilisé (conforme au Tableau 1 XP X43-132 ?), "
                        "la valeur de Kv (doit être calculé avec les bonnes données de QAL1), "
                        "et si la VLE utilisée est la VLE réglementaire journalière ou horaire."
                    ),
                    "NF EN 14181 §5.4.3 + XP X43-132 Tableau 1",
                    f"Vérifier les paramètres σ0, Kv et VLE utilisés dans le calcul pour {name}."
                )

    # ── Données en mg/Nm3 humide (dérogation §7.3) ──
    if any("humid" in d.lower() or "mg/nm3" in d.lower() for d in derogations):
        add(
            "Conditions de référence",
            "important",
            "Données SRM en mg/Nm3 gaz humide utilisées sans correction",
            (
                "Le rapport mentionne l'utilisation de fichiers minutes en mg/Nm3 sur gaz humide sans correction "
                "vers les conditions normalisées (gaz sec, 273K, 101,3 kPa, O2 de référence). "
                "La norme NF EN 14181 impose que les comparaisons AMS/SRM soient réalisées dans les mêmes "
                "conditions de référence. L'utilisation de données non normalisées est une dérogation qui peut "
                "biaiser systématiquement toutes les fonctions d'étalonnage."
            ),
            "NF EN 14181 §5.4.1 + NF X 43-551",
            "Demander la vérification de la normalisation des données SRM pour chaque polluant."
        )

    # ── Point positif : appareils ENVEA conformes ──
    valid_reg = [ch for ch in channels if ch.get("valid") and ch.get("regulated")]
    if valid_reg:
        names = ", ".join(ch.get("name", "?").upper() for ch in valid_reg)
        add(
            "Conformité AMS",
            "info",
            f"Canaux ENVEA conformes : {names}",
            (
                f"Les canaux {names} satisfont aux critères NF EN 14181 / XP X43-132. "
                "Les fonctions d'étalonnage validées démontrent l'aptitude des AMS ENVEA pour ces polluants."
            ),
            "NF EN 14181 §5.4",
            "Mettre à jour les cartes QAL3 avec les nouvelles fonctions d'étalonnage."
        )

    return points


# ─── Score AMS ────────────────────────────────────────────────────────────────

def _compute_ams_score(channels, regulated_count, regulated_valid) -> float:
    if regulated_count == 0:
        return 3.0
    ratio = regulated_valid / regulated_count
    critical = sum(1 for c in channels for i in c.get("issues", []) if i.get("severity") == "critical")
    score = ratio * 5.0 - (critical * 0.4)
    return max(1.0, min(5.0, round(score * 2) / 2))


# ─── Plan d'action ────────────────────────────────────────────────────────────

def _generate_actions(rapport, channels, meta, ops, resp_times) -> list:
    actions = []
    n = [0]

    def add(action, pollutant, target, cat, priority, delay, ref):
        n[0] += 1
        actions.append({
            "num": n[0], "action": action, "pollutant": pollutant,
            "target": target, "category": cat, "priority": priority,
            "delay": delay, "ref": ref
        })

    # ── Actions canaux non conformes ──
    for ch in channels:
        name = (ch.get("name") or "?").upper()
        for issue in ch.get("issues", []):
            sev = issue.get("severity", "warn")
            if sev == "critical":
                add(
                    f"CRITIQUE — {name} : {issue['label'][:100]}. Vérification immédiate de l'AMS requise. Contester les résultats du labo si pente négative ou R² < 0,1.",
                    name, "ENVEA + Exploitant", "AMS", "P1", "Immédiat", issue["ref"]
                )
            elif sev == "error" and not ch.get("valid"):
                add(
                    f"{name} NON CONFORME — {issue['label'][:100]}. Analyser la cause racine (AMS ou méthode SRM) avant décision.",
                    name, "Exploitant + ENVEA", "AMS", "P1", "< 1 mois", issue["ref"]
                )
            elif sev == "warn" and not ch.get("valid"):
                add(
                    f"{name} — {issue['label'][:100]}. Corriger avant le prochain AST.",
                    name, "Exploitant", "AMS", "P2", "< 3 mois", issue["ref"]
                )

    # ── Temps de réponse non conformes ──
    nc_tr = [t for t in resp_times if t.get("non_conforme")]
    if nc_tr:
        direct = [t for t in nc_tr if t.get("direct_injection")]
        if direct:
            params = ", ".join(t["param"].upper() for t in direct)
            add(
                f"Contester formellement la non-conformité TR {params} : injection en direct analyseur ≠ procédure XP X43-132. Exiger retest en bout de sonde.",
                params, "Exploitant → Laboratoire", "LABO", "P1", "< 2 semaines", "XP X43-132 §5.4"
            )
        other = [t for t in nc_tr if not t.get("direct_injection")]
        if other:
            params2 = ", ".join(t["param"].upper() for t in other)
            add(
                f"Vérifier et optimiser le temps de réponse {params2} : purge ligne, volume mort, longueur ligne.",
                params2, "ENVEA Techniciens", "AMS", "P2", "< 3 mois", "XP X43-132 §5.4"
            )

    # ── Documentation ──
    doc_ops = ops.get("documentation", {})
    if doc_ops.get("present") and doc_ops.get("nb_manquant", 0) > 0:
        items = doc_ops.get("items", {})
        missing = [k for k, v in items.items() if v == "NON"]
        add(
            f"Compléter la documentation AMS manquante : {', '.join(missing[:5])}. "
            "Fournir exports GLPI et rapports maintenance ENVEA comme justificatifs.",
            "Documentation", "Exploitant + ENVEA", "DOCS", "P2", "< 2 mois", "NF EN 14181 §5.3.2"
        )

    # ── MR non-extractifs ──
    mr_ops = ops.get("mr_non_extractif", {})
    if mr_ops.get("present") and not mr_ops.get("realise"):
        add(
            "Planifier les injections MR sur AMS poussières (non réalisées §9.8). "
            "À intégrer dans la prochaine maintenance préventive ENVEA.",
            "Poussières", "ENVEA", "AMS", "P2", "< 6 mois", "NF EN 14181 §5.3.7"
        )

    # ── Dérogations ──
    for crit in rapport.get("criteres", []):
        if crit["status"] == "ko" and "dérogation" not in crit["label"].lower():
            add(
                f"Rapport labo — {crit['label'][:120]}. Contacter le laboratoire pour correction ou justification.",
                "Rapport", "Exploitant → Laboratoire", "LABO", "P1", "< 2 semaines", crit["ref"]
            )

    # ── Actions standard ──
    valid_channels = [ch for ch in channels if ch.get("valid")]
    if valid_channels:
        add(
            "Mettre à jour les cartes de contrôle QAL3 (Shewhart) avec les nouvelles fonctions d'étalonnage validées. "
            "Transmettre au responsable QAL3 de l'exploitant.",
            "Canaux conformes", "Exploitant + ENVEA", "DOCS", "P2", "< 2 mois", "NF EN 14181 §6"
        )
        add(
            "Surveiller hebdomadairement le domaine de validité d'étalonnage. "
            "Déclencher nouveau QAL2 si les concentrations dépassent le domaine valide pendant > X jours consécutifs.",
            "Canaux conformes", "Exploitant", "EXPLOIT", "P2", "Continu", "XP X43-132 §9"
        )

    add(
        "Transmettre le rapport QAL2 à l'inspection (DREAL/DRIEAT) avec le plan d'actions correctives. "
        "Documenter les points contestés et les actions ENVEA planifiées.",
        "Tous", "Exploitant", "DOCS", "P2", "< 1 mois", "Arrêté préfectoral"
    )
    add(
        "Planifier le prochain AST (canaux valides) et les nouvelles campagnes QAL2 (canaux invalides ou contestés). "
        "Prévoir la présence d'un technicien ENVEA lors du prochain QAL2.",
        "Tous", "Exploitant + ENVEA + Labo", "EXPLOIT", "P3", "< 12 mois", "NF EN 14181 §7"
    )

    return actions


# ─── Vérifications normes ─────────────────────────────────────────────────────

def check_norm_updates() -> list:
    return [
        {
            "norm": norm,
            "current_version": info["current"],
            "reference": info["ref"],
            "status": "current",
            "message": f"Version en vigueur : {info['ref']}",
            "source": "Base de connaissance ENVEA"
        }
        for norm, info in NORM_VERSIONS.items()
    ]


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _score_to_stars(score: float) -> str:
    full  = int(score)
    half  = 1 if (score - full) >= 0.5 else 0
    empty = 5 - full - half
    return "★" * full + ("½" if half else "") + "☆" * empty


def _score_color(score: float) -> str:
    if score >= 4.0: return "success"
    if score >= 3.0: return "warning"
    return "danger"
