"""Separate an explicit match-availability statement from the scene of an injury."""

import re


COMPETITION = re.compile(
    r"\b(?:champions league|europa league|conference league|fa cup|league cup|carabao cup|"
    r"efl cup|world cup|international (?:duty|break|fixture|match|friendly)|"
    r"national team|nations league|european championship|uefa|friendl(?:y|ies)|"
    r"youth league|under[- ](?:18|21|23)|u(?:18|21|23)|women['’]s)\b", re.I,
)
_INJURY = re.compile(
    r"\b(?:injur\w*|hurt|suffer\w*|sustain\w*|pick(?:ed|ing) up|knock|strain\w*|"
    r"sprain\w*|fractur\w*|twist\w*|tore|torn|limp\w*|ligament|concussion)\b", re.I,
)
_DECISION = re.compile(
    r"\b(?:will (?:not |now |also )?(?:miss|play|feature|face|start|be)|would (?:miss|sideline)|"
    r"(?:set|expected|likely) to miss|ruled out|unavailable|available|doubtful|"
    r"fit (?:to|for)|sidelined|out of|back in the squad)\b", re.I,
)
MATCH_TARGET = re.compile(
    r"\b(?:premier league|premiership|epl|against|versus|vs\.?|match|game|fixture|clash|"
    r"trip to|visit of|(?:coming|upcoming|rest of the|remainder of the) season|"
    r"(?:days?|weeks?|months?)|this weekend)\b", re.I,
)
_BOUNDARY = re.compile(
    r",\s*|\s+\b(?:after|following|having|despite|because of|due to|"
    r"and(?:\s+(?:so|therefore|now))?|but)\s+", re.I,
)


def injury_headline(title, target):
    """A title naming the injury scene can accompany an explicit league decision."""
    return (_INJURY.search(title) and COMPETITION.search(title) and not _DECISION.search(title)
            and _DECISION.search(target) and re.search(r"\b(?:premier league|premiership|epl)\b", target, re.I))


def competition_target(text):
    """Return only the target clause when another competition describes a medical incident."""
    if not COMPETITION.search(text):
        return text
    for boundary in _BOUNDARY.finditer(text):
        before, after = text[:boundary.start()], text[boundary.end():]
        for main, background in ((before, after), (after, before)):
            if (not COMPETITION.search(main) and _DECISION.search(main) and MATCH_TARGET.search(main)
                    and COMPETITION.search(background) and _INJURY.search(background)
                    and not _DECISION.search(background)):
                return main
    return None
