"""Dated field-level source permissions. Live field coverage needs a real receipt."""

from . import league_registry as registry

REVIEWED = "2026-11-26"
SEASON = "2026-27"
LANG = {"epl": "en", "lal": "es", "bun": "de", "sea": "it", "fl1": "fr"}

# Field-level grants are intentionally independent of collection evidence. Every
# club points to its applicable grants and limitations, not to a blanket licence.
SOURCES = {
    "openfootball": {
        "publisher": "OpenFootball", "fields": ["regular_fixture", "retrospective_regulation_score"],
        "allowed": ["fetch_pinned_version", "retain", "model_admitted_scores", "redistribute"],
        "rights": "CC0 1.0", "url": registry.SOURCE_ROOT,
        "terms": [f"{registry.SOURCE_ROOT}/LICENSE.md"],
        "conditions": "Check ordered teams, stage, status and date. Mirrors and upstream TXT have shared lineage.",
    },
    "wikidata": {
        "publisher": "Wikidata contributors", "fields": ["entity_id", "alias", "qualified_club_membership"],
        "allowed": ["fetch_structured_main_namespace", "retain", "identity_resolution", "redistribute"],
        "rights": "CC0 1.0 for structured main-namespace data",
        "url": "https://www.wikidata.org/w/api.php", "terms": ["https://www.wikidata.org/wiki/Wikidata:Licensing"],
        "conditions": "Inspect competition, qualifiers, dates, subject identity and origin per statement.",
    },
    "wikipedia": {
        "publisher": "Wikipedia contributors", "fields": ["season_roster_candidate", "reported_team_news_candidate", "result_candidate"],
        "allowed": ["retrieve_licensed_revisions", "retain_with_attribution", "extract_eligible_facts"],
        "rights": "Normally CC BY-SA 4.0 for eligible page text",
        "url": "https://www.mediawiki.org/wiki/API:Revisions",
        "terms": ["https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use",
                  "https://www.mediawiki.org/wiki/Wikimedia_APIs/Content_reuse"],
        "conditions": "Record every particular page/revision, authors, licence, edits and template dependencies. Linked articles, images and third-party quotations require their own grant. An encyclopedia roster cannot establish a matchday XI.",
    },
    "gdelt": {
        "publisher": "GDELT Project", "fields": ["index_metadata", "publisher_link"],
        "allowed": ["discover_sources", "attribute_and_link"], "rights": "GDELT indexed metadata",
        "url": "https://api.gdeltproject.org/api/v2/doc/doc",
        "terms": ["https://www.gdeltproject.org/about.html"],
        "conditions": "Discovery only. Recheck the article publisher before any automated article reuse.",
    },
    "touchline": {
        "publisher": "Touchline Wire", "fields": ["reported_availability", "labelled_rumour"],
        "allowed": ["free_eligible_project_api", "retain_attributed_fields", "extract", "model_eligible_facts"],
        "rights": "Free for personal, hobbyist, academic and open-source projects under the provider's conditions",
        "url": "https://touchlinewire.com/docs", "terms": ["https://touchlinewire.com/docs#attribution", "https://touchlinewire.com/terms.html"],
        "conditions": "Credit and link Tactical Data by Touchline Wire. Confirm use scope and endpoint membership. Scope alone cannot attest any club's latest report, absence, or a confirmed starting XI.",
    },
    "openligadb-bl1": {
        "publisher": "OpenLigaDB community", "fields": ["fixture_candidate", "score_candidate"],
        "allowed": ["api_with_shared_cache", "retain", "research_with_attribution"],
        "rights": "ODbL 1.0 data and separate API use terms",
        "url": "https://api.openligadb.de/getmatchdata/bl1/2026",
        "terms": ["https://www.openligadb.de/en/license", "https://www.openligadb.de/en/terms-of-use"],
        "conditions": "Respect API policy and ODbL attribution/share-alike terms on derivative databases. Match ID, field and original source lineage require verification.",
    },
    "interno": {
        "publisher": "Ministero dell'Interno", "fields": ["stadium_access_order"],
        "allowed": ["retrieve_eligible_public_notices", "retain_eligible_text_with_attribution", "extract_scope"],
        "rights": "CC BY 4.0 for own content, subject to exceptions",
        "url": "https://www.interno.gov.it/it/notizie", "terms": ["https://www.interno.gov.it/it/note-legali"],
        "conditions": "Verify the dated notice's rights, fixture, geographic scope and exceptions. Exclude third-party material. The default is a candidate source, not a per-club article grant.",
    },
    "clausefoot": {
        "publisher": "Clause Foot", "fields": ["discovery_headline", "original_link"],
        "allowed": ["poll_rss_for_titles", "display_title_with_credit_and_original_link"],
        "rights": "Published RSS title-link terms",
        "url": "https://clausefoot.com/flux-rss/", "terms": ["https://clausefoot.com/flux-rss/"],
        "conditions": "No validated per-team coverage, durable article archive or full-text training grant. Check publisher conditions and attribute.",
    },
}
MANUAL = {
    "epl": ("Premier League", "https://www.premierleague.com/en/terms-and-conditions"),
    "lal": ("LaLiga", "https://www.laliga.com/en-ES/legal/legal-web"),
    "bun": ("DFL", "https://www.bundesliga.com/en/bundesliga/info/imprint"),
    "sea": ("Lega Serie A", "https://www.legaseriea.it/terms-and-conditions"),
    "fl1": ("LFP", "https://ligue1.com/fr/legal/cgu"),
}

FIELD_KEYS = ("identity", "schedule_and_result", "roster", "player_availability", "rumours",
              "disciplinary_order", "confirmed_matchday_squad_and_xi", "team_article_fulltext",
              "off_field_orders")
COMMON_FIELDS = {
    "identity": ["wikidata", "openfootball"], "schedule_and_result": ["openfootball", "wikipedia"],
    "roster": ["wikipedia", "wikidata"], "player_availability": [], "rumours": [],
    "disciplinary_order": [], "confirmed_matchday_squad_and_xi": [], "team_article_fulltext": [],
    "off_field_orders": [],
}


def rights_register(leagues=None, *, season=SEASON):
    from .news_scope import wire_coverage

    if season != SEASON:
        raise ValueError(f"Field rights reviewed only for {SEASON}; see league membership for historical teams")
    leagues = list(registry.PROFILES) if leagues is None else list(leagues)
    profiles, clubs = {}, []
    for league in leagues:
        registry.profile(league)
        fields = {k: list(v) for k, v in COMMON_FIELDS.items()}
        if league == "epl":
            fields["player_availability"].append("touchline")
            fields["rumours"].append("touchline")
        elif league == "bun":
            fields["schedule_and_result"].append("openligadb-bl1")
        elif league == "sea":
            fields["off_field_orders"].append("interno")
        discovery = ["gdelt", "clausefoot"] if league == "fl1" else ["gdelt"]
        publisher, terms = MANUAL[league]
        profiles[league] = {
            "fields_to_candidate_sources": fields, "metadata_discovery_only": discovery,
            "local_language": LANG[league], "uncovered_fields": [k for k, v in fields.items() if not v],
            "manual_fallback": {"publisher": publisher, "terms": terms,
                                "use": "Verify the specific club or authority's dated notice manually. Club sites have independent terms; no automated full-text grant in this register."},
        }
        for raw in registry.members(league, season):
            club = registry.club_key(league, season, raw)
            row = {"league": league, "club_id": f"{league}/{club}", "club": raw, "rights_profile": league,
                   "membership_proof": f"{registry.SOURCE_ROOT}/{season}/{registry.profile(league).source_file}",
                   "last_valid_team_receipt_utc": None, "window_field_coverage": "requires_actual_receipts"}
            if league == "epl":
                proof = wire_coverage(raw, season, league)
                if not proof:
                    raise ValueError(f"Published Touchline membership does not cover {row['club_id']}")
                row["touchline_directory"] = {k: proof[k] for k in ("membership_sha256", "valid_from", "valid_until", "membership_url")}
            clubs.append(row)
    used = {key for p in profiles.values() for candidates in p["fields_to_candidate_sources"].values() for key in candidates}
    used.update(key for p in profiles.values() for key in p["metadata_discovery_only"])
    return {"rights_review_date": REVIEWED, "season": season, "stage": "regular", "team_count": len(clubs),
            "field_order": FIELD_KEYS, "sources": {k: SOURCES[k] for k in sorted(used)},
            "league_rights": profiles, "clubs": clubs,
            "health_scope": "Static membership/permissions inventory. A null live receipt asks operators to inspect their own collector; it cannot be used to infer health, absence, roster completeness or timely match coverage.",
            "source_review_trigger": "Re-check these permissions, coverage and terms after an upstream policy/field change or at season rollover. Only admitted local collectors supply live status."}
