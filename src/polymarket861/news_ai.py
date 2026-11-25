"""Grounded English squad-news extraction. Fully local, with no model download.

A supervised multinomial naive Bayes classifier learns event, availability and
framing from original annotated examples. An entity linker restricts subjects to
the supplied club roster. Deterministic scope, polarity and source checks can
abstain or weaken an inference. The classifier score describes language fit,
not a player's probability of appearing or an adjustment to goal rates.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files

from .news_ai_corpus import TRAINING_GROUPS
from .store import digest

MODEL_VERSION = "squad-news-en-nb-1-" + digest({
    "training": TRAINING_GROUPS,
    "extractor": hashlib.sha256(files(__package__).joinpath("news_ai.py").read_bytes()).hexdigest(),
})[:16]
_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*")
_TRANSLITERATE = str.maketrans({"ø": "o", "ł": "l", "æ": "ae", "œ": "oe", "đ": "d", "ð": "d", "ı": "i"})
_STOP = {"player", "a", "an", "the", "of", "to", "and", "for", "his", "on", "in", "at", "is", "was"}
_LEMMAS = {
    "injuries": "injury", "injured": "injury", "injures": "injury", "injuring": "injury",
    "returning": "return", "returns": "return", "returned": "return",
    "recovering": "recover", "recovered": "recover", "recoveries": "recover",
    "suspended": "suspension", "suspensions": "suspension",
    "banned": "ban", "bans": "ban", "misses": "miss", "missed": "miss",
    "confirmed": "confirm", "confirms": "confirm",
    "rumors": "rumour", "rumor": "rumour", "rumours": "rumour", "rumored": "rumoured",
    "reports": "report", "reported": "report", "sidelining": "sidelined",
    "denies": "deny", "denied": "deny", "claims": "claim", "knocks": "knock",
}


def _fold(text):
    return "".join(
        c for c in unicodedata.normalize("NFKD", text.casefold().translate(_TRANSLITERATE))
        if not unicodedata.combining(c)
    ).replace("’", "'").replace("‑", "-").replace("–", "-")


def _features(text):
    words = [_LEMMAS.get(w, w) for w in _WORD.findall(_fold(text))]
    # A term contributes once; repeated names and boilerplate cannot add votes.
    return {w for w in words if w not in _STOP} | {
        left + " " + right for left, right in zip(words, words[1:])
        if left not in _STOP or right not in _STOP
    }


# Evidentiality is learned from attribution language. The name, disease and
# match consequences cannot identify the source of a report.
_FRAME_WORDS = {
    "rumour", "rumoured", "speculation", "speculate", "supporters", "unverified",
    "unconfirmed", "gossip", "fans", "social", "fan", "unsubstantiated", "confirmation",
    "report", "reporter", "reportedly", "according", "understood", "believed", "journalist",
    "media", "newspaper", "paper", "said", "claim", "deny", "refuted", "dismissed",
    "false", "incorrect", "untrue", "debunked", "confirm", "club", "coach", "manager",
}


def _framing_features(features):
    words = features & _FRAME_WORDS
    return words or {"declarative clause"}


class _Classifier:
    def __init__(self, records):
        counts = defaultdict(Counter)
        vocab = set()
        for label, words in records:
            counts[label].update(words)
            vocab.update(words)
        self.vocab = vocab
        self.log_p = {}
        for label, count in counts.items():
            total = sum(count.values()) + len(vocab) * 0.6
            self.log_p[label] = {word: math.log((count[word] + 0.6) / total) for word in vocab}

    def distribution(self, features):
        known = sorted(features & self.vocab)
        scores = {label: sum(weights[word] for word in known) for label, weights in self.log_p.items()}
        best = max(scores.values())
        odds = {label: math.exp(score - best) for label, score in scores.items()}
        total = sum(odds.values())
        return {label: value / total for label, value in odds.items()}

    def predict(self, features):
        weights = self.distribution(features)
        label = max(weights, key=weights.get)
        return label, weights[label]


@lru_cache(maxsize=1)
def _models():
    samples = [
        ((event, availability, framing), _features(text))
        for event, availability, framing, examples in TRAINING_GROUPS for text in examples
    ]
    return (_Classifier((labels[0], words) for labels, words in samples),
            _Classifier((labels[1], words) for labels, words in samples),
            _Classifier((labels[2], _framing_features(words)) for labels, words in samples))


def _name_word(token):
    return _fold(token).removesuffix("'s").replace("'", "")


def _name_key(text):
    return tuple(filter(None, (_name_word(token) for token in _WORD.findall(text))))


@dataclass(frozen=True)
class _Mention:
    start: int
    end: int
    ident: str


def _roster_index(roster):
    aliases = defaultdict(set)
    players = {}
    for row in roster:
        ident = row.get("player_id")
        name = row.get("player_name") or row.get("name")
        if not isinstance(ident, str) or not ident or not isinstance(name, str) or not name.strip():
            continue
        players[ident] = (name, row.get("position"))
        declared = row.get("aliases") or []
        if isinstance(declared, str):
            declared = [declared]
        surname = _name_key(name.split()[-1])
        if surname and len("".join(surname)) >= 3:
            aliases[surname].add(ident)
        for alias in (name, *declared):
            if isinstance(alias, str):
                key = _name_key(alias)
                if key and (len(key) > 1 or len(key[0]) >= 3):
                    aliases[key].add(ident)
    return players, aliases, max(map(len, aliases), default=0)


# Capitalized words that do not imply an unrecognised first name before a
# surname-only match. This protects e.g. the unrelated "Colin Rice".
_NAME_PREFIX = {"while", "whereas", "but", "and", "though", "meanwhile", "with", "for", "that", "now", "captain", "striker", "defender", "goalkeeper", "winger", "midfielder"}


def _mentions(text, aliases, width):
    tokens = list(_WORD.finditer(text))
    keys = [_name_word(token.group()) for token in tokens]
    result = []
    i = 0
    while i < len(tokens):
        found = False
        for size in range(min(width, len(tokens) - i), 0, -1):
            ids = aliases.get(tuple(keys[i:i + size]))
            if not ids:
                continue
            # Never resolve the shorter end of an ambiguous longer name.
            start, end = tokens[i].start(), tokens[i + size - 1].end()
            between = text[start:end]
            good = len(ids) == 1 and not re.search(r"[,;:.!?/\n]", between)
            if i and text[tokens[i-1].end():start] in ("-", "‐", "‑", "–"):
                good = False  # One component of another person's compound name.
            if size == 1:
                good = good and tokens[i].group()[0].isupper()
                if i and (tokens[i - 1].group()[0].isupper() and keys[i - 1] not in _NAME_PREFIX
                          and not _fold(tokens[i - 1].group()).endswith("'s")
                          and text[tokens[i - 1].end():start].strip() == ""):
                    good = False
            if good:
                # Exclude the English possessive from the grounded name.
                raw = text[start:end]
                suffix = 2 if raw.casefold().endswith(("'s", "’s")) else 0
                result.append(_Mention(start, end - suffix, next(iter(ids))))
            i += size
            found = True
            break
        if not found:
            i += 1
    return result


# The scope check requires a football predicate close to the linked subject.
_AUX = r"(?:am|is|are|was|were|be|being|been|has|had|have|will|would|shall|may|might|could|couldn't|can't|cannot|won't|isn't|aren't|wasn't|hasn't|haven't|should|did|does|not|no|longer|to|a|an|the|at|still|both|each|also|now|just|already|recently|again|fully|currently|definitely|potentially|reportedly|reported|understood|believed|said|likely|unlikely|expected|set|due|remain|remains|remained|appear|appears|seem|seems|looking|been)"
_PRED = r"(?:injur\w*|sick|ill|hurt|suffer\w*|sustain\w*|pick(?:ed)? up|recover\w*|return\w*|resume\w*|rejoin\w*|back|miss\w*|ruled out|rule\w* out|out\b|sidelin\w*|absen\w*|availab\w*|unavailab\w*|fit\b|fitness|ready|suspend\w*|suspension|ban\w*|serv\w*|clear\w*|doubt\w*|assess\w*|fac(?:es?|ing)\b|take no part|leave\b|join\b|linked\b|play\b|feature\b|participate\b|sit\w* out|left out|named in|in the (?:matchday )?squad|touch and go|fifty.fifty|undergo\w*|receiv\w*|train\w*|hamstring|knee|calf|knock|ankle|muscle|head injury|thigh|shoulder|groin|virus|a[n]? (?:knock|injury|problem))"
_SUBJECT = re.compile(r"^(?:'s\s+)?(?:(?:both of whom|who|whom|the captain)\s+)?(?:" + _AUX + r"\s+){0,12}" + _PRED + r"\b")
_OBJECT = re.compile(r"(?:\bwithout|\b(?:injury|knock) to|\bfitness (?:of|test for)|\b(?:suspended|banned|injured|recovering|sidelined)|\b(?:ban|rumou?r|speculation) (?:about|over|regarding)|\b(?:ruled|ruling|rules)|\bclear(?:ed)?|\bexpected to be without)\s*$")
_NEG = re.compile(r"\b(?:den(?:y|ies|ied)|refut(?:e|es|ed)|dismiss(?:es|ed)?|retract(?:s|ed)?|debunk(?:s|ed)?|unfounded|untrue|incorrect|baseless|false|withdraw(?:s|n)?|withdraws?|mistaken|erroneous|misreported|unfounded)\b")
_RUMOUR = re.compile(r"\b(?:rumou?r(?:s|ed)?|speculat\w*|unconfirmed|unverified|unsubstantiated|without confirmation|no (?:independent )?(?:evidence|confirmation)|not (?:yet )?confirm(?:ed)?|declined to confirm|gossip|social media claim\w*|fan accounts? claim\w*)\b")
_REPORTED = re.compile(r"\b(?:reportedly|reports? (?:say|suggest|claim)|according to|understood to|is reported|believed to|said to|press reports?|local media|newspaper|tabloid|not confirmed|unaware of)\b")
_SOFT = re.compile(r"\b(?:might|may|could|possibly|potentially|unlikely|expected|likely|in contention|in the frame|hop(?:e|es|eful|ing)|not ruled out|not been ruled out|whether)\b")
_DUBIOUS = re.compile(r"\b(?:doubt(?:ful)?|fitness (?:test|check)|assess(?:ment|ed)?|touch and go|fifty.fifty|50.50|late (?:check|test|decision)|uncertain|in doubt|in contention|in the frame)\b")
_INJURY = re.compile(r"\b(?:injur\w*|sick|ill|illness|virus|concussion|hamstring|calf|ankle|knock|fractur\w*|surgery|tendon|ligament|muscle|knee|thigh|strain|groin|shoulder|sprain\w*|rehab\w*|operation|treatment|hurt)\b")
_DIS = re.compile(r"\b(?:suspend\w*|suspens\w*|ban(?:ned|s)?|serv(?:e|es|ing) (?:a |his )?(?:one|two|three|four|five).match)\b")
_RETURN = re.compile(r"\b(?:return(?:s|ed|ing)?|resum(?:e|es|ed|ing)|rejoin\w*|back in|back to|recover\w*|cleared to|fit again|available again)\b")
_OUT = re.compile(r"\b(?:ruled out|rule[sd]? (?:out|player out)|out (?:of|with|through|due to|because|injured|for)|remain(?:s|ed)? out|sidelin\w*|will be without|(?:is|are) without|unavailable|not (?:be )?(?:available|fit|ready|play|feature|participate)|won't (?:be available|play|feature)|will be absent|take no part|left out of|sit(?:s|ting)? out|will miss|would miss|misses out|miss(?:es)? (?:the |a |this |next |sunday|monday|tuesday|wednesday|thursday|friday|saturday)|keep\w* player out|out until)\b")
_AVAILABLE = re.compile(r"\b(?:available(?: again)?(?: for (?:selection|the |this |saturday|sunday|monday|tuesday|wednesday|thursday|friday)| to (?:play|face))?|fit(?: again)? (?:to (?:play|face|return|start|feature)|for (?:the |this |saturday|sunday|squad|selection))|cleared to (?:play|return|start|feature)|(?:back in|returns? to|returned to|named in|in) the (?:matchday )?squad|fit again)\b")
_IRRELEVANT = re.compile(r"\b(?:injury.time|stoppage.time|ruled offside|transfer(?:s|red)?|free agent|miss(?:es|ed)? (?:a |the |his |an )?(?:penalty|target|chance|flight|award|bus|opportunity)|ruled out (?:a |the |his )?(?:goal|appeal|move|sale|transfer)|returned? (?:the |a |his )?pass|fit for purpose|suspended (?:fine|sentence)|goals? ruled out)\b")
_OTHER_COMP = re.compile(r"\b(?:champions league|europa league|conference league|fa cup|league cup|carabao cup|efl cup|international (?:duty|fixture|match)|world cup|european championship|nations league|uefa|under.21|u21|u18)\b")
_PREMIER = re.compile(r"\b(?:premier league|premiership|epl)\b")
_PAST = re.compile(r"\b(?:last (?:season|year|month|night|weekend|week|match|game|saturday|sunday)|years? ago|months? ago|as a youngster|in (?:19|20)\d\d|in (?:january|february|march|april|may|june|july|august|september|october|november|december) (?:19|20)\d\d|was substituted|after (?:the|their) defeat|scored|equaliser|equalizer|at the time|had missed|previously missed|missed (?:the|their|last)|former club|last time|in the reverse fixture)\b")
_FORWARD = re.compile(r"\b(?:now|still|remains?|next|upcoming|ahead of|will (?:not |be |miss|serve|sit|face|return)|ruled out for|this weekend|this week|for selection)\b")
_HYPOTHETICAL = re.compile(r"\b(?:if|suppose|imagine|hypothetical|what if|in case|were to|ignore (?:all |previous |prior )?instructions|pretend)\b")
_PERIOD = re.compile(r"[.!?]+(?=\s|$)|[;\n]+|$")


def _ranges(text, boundary):
    start = 0
    for end in boundary.finditer(text):
        stop = end.end()
        if stop > start:
            yield start, stop
        start = stop


def _split_subjects(text, mentions):
    """One predicate for a coordinated list, or two explicitly separated clauses."""
    group = [mentions[0]]
    left = 0
    for nxt in mentions[1:]:
        prev = group[-1]
        between = text[prev.end:nxt.start]
        if re.fullmatch(r"\s*(?:(?:,|and|&)\s*)+", between, re.I):
            group.append(nxt)
            continue
        separator = list(re.finditer(r",\s*(?:and\s+)?|\s+(?:and|while|but|whereas)\s+", between, re.I))
        if not separator:
            # Two subjects and no safe predicate boundary, e.g. "X discussed Y's injury".
            return
        cut = prev.end + separator[-1].start()
        yield left, cut, group
        left = prev.end + separator[-1].end()
        group = [nxt]
    yield left, len(text), group


def _subject_link(text, first, last, nomination):
    suffix = _fold(text[last:]).strip(" ,–—:\"“”‘")
    prefix = _fold(text[:first])
    return bool(_SUBJECT.search(suffix) or _OBJECT.search(prefix)
                or (nomination and _LOCAL_SUBJECT.search(suffix)))


# Additional literal cues an optional local language model can nominate. Their
# presence gates a proposal; free-form model conclusions do not expand scope.
_LOCAL_INJURY = re.compile(r"\b(?:bruise[ds]?|broken (?:toe|wrist|leg|foot)|meniscus|twist(?:ed)? (?:his |an? )?(?:ankle|knee)|nursing a (?:bruised|swollen)|medical scans?)\b")
_LOCAL_OUT = re.compile(r"\b(?:left behind for the trip|will not (?:travel|make the (?:trip|journey))|omitted from the travelling party)\b")
_LOCAL_CLEARED = re.compile(r"\b(?:given the green light to play|medically cleared for action|free to play again)\b")
_LOCAL_RETURN = re.compile(r"\b(?:back on the grass|given the green light to play|medically cleared for action|free to play again)\b")
_LOCAL_SUBJECT = re.compile(r"^(?:(?:" + _AUX + r")\s+){0,10}(?:nursing|bruised|twisted|given the green light|medically cleared|travel|make the trip|omitted from the travelling party|free to play again|left behind)\b")


_OWNER = r"([A-ZÀ-Þ][\w.’'-]*(?:\s+(?:[A-ZÀ-Þ][\w.’'-]*|and|&|de|of)){0,4})"
_ROLE = r"(?:keeper|goalkeeper|captain|player|winger|forward|striker|defender|midfielder|centre.back|full.back)"
_OWNED = re.compile(_OWNER + r"['’]s\s+(?:(?:injured|suspended|senior|young|central)\s+)?(?:" + _ROLE + r"\s+)?$")
_CLUB_ROLE = re.compile(_OWNER + r"\s+" + _ROLE + r"\s+$")
_WITHOUT = re.compile(_OWNER + r"\s+(?:will be|are|is|remain) without\s+$")
_FOR_CLUB = re.compile(r"\bfor " + _OWNER)
_TRANSFER = re.compile(r"\b(?:could|might|may|set to|expected to) (?:leave|join)|\btransfer (?:away|rumou?r)\b|\blink\w* player with a transfer")
_DAY = re.compile(r"^(?:mon|tues?|wed(?:nes)?|thurs?|fri|satur|sun)day|^(?:january|february|march|april|may|june|july|august|september|october|november|december)$")
_GENERIC_OWNER = {"club", "the club", "team", "the team", "our", "the", "academy", "first team"}


def _club_words(label):
    parts = [s for s in _name_key(label) if s not in ("a", "the", "fc", "afc")]
    words = " ".join(parts)
    return {"man utd": "manchester united", "man united": "manchester united",
            "man city": "manchester city", "spurs": "tottenham hotspur",
            "wolves": "wolverhampton wanderers", "brighton": "brighton hove albion"}.get(words, words)


def _foreign_club(text, first, last, team):
    own = _club_words(team or "")
    def conflicting(label):
        candidate = _club_words(label)
        if label.casefold() in _GENERIC_OWNER or not candidate or _DAY.match(candidate):
            return False
        # The published short name may omit United, City or an FC suffix.
        return not (own and (candidate == own or own.startswith(candidate + " ") or candidate.endswith(" " + own)))
    prefix, after = text[:first], text[last:]
    for pattern in (_OWNED, _CLUB_ROLE, _WITHOUT):
        match = pattern.search(prefix)
        if match and conflicting(match[1]):
            return True
    for match in _FOR_CLUB.finditer(after):
        tail = after[match.end():].lstrip()
        if re.match(r"(?:clash|match|test|game|trip|visit|tie)\b", tail, re.I):
            continue
        if own and conflicting(match[1]):
            return True
    return False


_CONTRACTIONS = re.compile(r"\b(isn|wasn|aren|weren|hasn|haven|hadn|doesn|won|couldn|didn|shouldn)'t\b")
_EXPAND = {"isn": "is not", "wasn": "was not", "aren": "are not", "weren": "were not",
           "hasn": "has not", "haven": "have not", "hadn": "had not", "doesn": "does not",
           "won": "will not", "couldn": "could not", "didn": "did not", "shouldn": "should not"}


_DIRECTIVE = re.compile(
    r"\b(?:ignore (?:all |the )?(?:previous |prior |system |developer )?instructions|"
    r"(?:reveal|disclose|print|override) (?:your |the )?(?:hidden |system |developer )?prompt|"
    r"system prompt|execute (?:a |the |this )?tool call)\b", re.I,
)


def _judgement(clause, authority, title, nomination=None):
    normalized = _CONTRACTIONS.sub(lambda m: _EXPAND[m[1]], _fold(clause))
    if not normalized.strip() or "?" in normalized or _HYPOTHETICAL.search(normalized):
        return None
    if (_RUMOUR.search(normalized) and _TRANSFER.search(normalized)
            and not _NEG.search(normalized) and not _PAST.search(normalized)):
        event_model, _, _ = _models()
        event, score = event_model.predict(_features(normalized))
        if event == "transfer":
            return "transfer", "rumour", "unknown", score
    if (_IRRELEVANT.search(normalized)
            or (_OTHER_COMP.search(normalized) and not _PREMIER.search(normalized))):
        return None
    if (_PAST.search(normalized) or re.search(r"\b(?:match report|highlights)\b", title)) and not _FORWARD.search(normalized):
        return None
    injury = bool(_INJURY.search(normalized))
    discipline = bool(_DIS.search(normalized))
    returning = bool(_RETURN.search(normalized))
    appears_out = bool(_OUT.search(normalized) or re.search(r"\b(?:is|are|ruled) out[.!;,\s]*$", normalized))
    available = bool(_AVAILABLE.search(normalized))
    uncertain = bool(_DUBIOUS.search(normalized))
    if nomination:
        if nomination == "injury" and _LOCAL_INJURY.search(normalized):
            injury = True
        elif nomination == "availability" and _LOCAL_OUT.search(normalized):
            appears_out = True
        elif nomination == "return" and _LOCAL_RETURN.search(normalized):
            returning = True
            available = bool(available or _LOCAL_CLEARED.search(normalized))
    if not (injury or discipline or returning or appears_out or available or uncertain):
        return None

    feats = _features(normalized)
    event_model, availability_model, framing_model = _models()
    event_scores = event_model.distribution(feats)
    event = max(event_scores, key=event_scores.get)
    event_score = event_scores[event]
    readiness, _ = availability_model.predict(feats)
    framing, frame_score = framing_model.predict(_framing_features(feats))
    # Require literal supporting language for each event type. Training attendance
    # alone cannot imply match fitness and a single card cannot establish a ban.
    events = []
    if injury:
        events.append("injury")
    if discipline:
        events.append("suspension")
    if returning and (injury or available or re.search(r"\b(?:training|squad|action|surgery|group)\b", normalized)
                      or nomination == "return" and _LOCAL_RETURN.search(normalized)):
        events.append("return")
    if appears_out or available or uncertain:
        events.append("availability")
    if not events:
        return None
    if nomination:
        if nomination not in events:
            return None
        event = nomination
    elif event == "none" and event_score > 0.87:
        return None
    if returning and ("return" in events) and (available or re.search(r"\b(?:training|squad)\b", normalized)):
        event = "return"
    elif event not in events or (event == "availability" and (injury or discipline)):
        event = events[0]

    event_score = event_scores[event]
    negated_claim = re.search(
        r"\b(?:no (?:longer (?:injured|hurt|suspended|banned)|truth (?:in|to) (?:the )?(?:claims?|reports?|suggestion|rumou?rs?))|not (?:actually )?(?:injured|hurt|sick|suspended|banned|unavailable|miss)|"
        r"not (?:actually )?(?:suffer(?:ed)?|sustain(?:ed)?|pick(?:ed)? up) (?:a|an|any) (?:\w+ ){0,2}(?:injury|knock|strain)|"
        r"no(?:t suffered an?| known)? injur(?:y|ies)|without (?:an )?injury|ban (?:has been )?(?:overturned|rescinded))\b",
        normalized,
    )
    denial = _NEG.search(normalized)
    if denial or negated_claim:
        # A rejected absence, return or diagnosis is not proof of its converse.
        return event, "denied", "unknown", event_score
    negative_return = re.search(r"\b(?:not (?:yet )?(?:back|recover\w*|return\w*|clear\w*)|yet to (?:recover|return)|not been cleared)\b", normalized)
    soft = bool(_SOFT.search(normalized))
    if uncertain or (soft and (appears_out or returning or available)):
        readiness = "doubtful"
    elif negative_return:
        readiness = "unknown"
    elif event == "return" and available and not re.search(r"\b(?:not|unavailable|ruled out)\b", normalized):
        readiness = "available"
    elif appears_out or (discipline and (readiness == "out" or re.search(r"\b(?:is|are|remains|been|reported) (?:suspended|banned)|\bserv(?:e|es|ing)\b", normalized))):
        readiness = "out"
    elif available:
        readiness = "available"
    else:
        readiness = "unknown"

    if _RUMOUR.search(normalized) or (framing == "speculation" and frame_score > 0.7) or _RUMOUR.search(title):
        status = "rumour"
    elif authority != "official" or _REPORTED.search(normalized) or soft or (framing in ("attributed", "speculation", "denial") and not re.search(r"\bconfirm(?:s|ed)?\b", normalized)):
        status = "reported"
    else:
        status = "confirmed"
    return event, status, readiness, event_score


_CONTRAST = re.compile(r"\s*,?\s*\b(?:while|whereas|but|however|although)\b[, ]*", re.I)
_CARRY = re.compile(r"^(?:(?:he|she|they)\s+)?(?:(?:is|are|has|have|can|will|was|were)\s+)?(?:now |still |since )?(?:back in the squad|fit to |available for |cleared to |returned to the (?:matchday )?squad|ruled out|unavailable|sustain(?:ed|s)?\b|suffer(?:ed|s)?\b|picked up\b|miss the (?:game|match)|injured\b)", re.I)


def _sentence_claims(sentence, offset, roster, authority, title, team, nomination):
    players, aliases, width = roster
    if _HYPOTHETICAL.search(_fold(sentence)) or sentence.rstrip().endswith("?"):
        return []
    ranges = []
    at = 0
    for split in _CONTRAST.finditer(sentence):
        if split.start() > at:
            ranges.append((at, split.start()))
        at = split.end()
    ranges.append((at, len(sentence)))
    claims = []
    prior = None
    for left, right in ranges:
        chunk = sentence[left:right]
        mentions = _mentions(chunk, aliases, width)
        if not mentions:
            if prior and _CARRY.match(chunk.lstrip()):
                ident, start, origin, local_title = prior
                result = _judgement("PLAYER " + chunk, origin, local_title, nomination)
                if result:
                    claims = [r for r in claims if r["player_id"] != ident]
                    claims.append(_record(sentence, offset, start, right, ident, players, result))
            prior = None
            continue
        prior = None
        for begin, end, group in _split_subjects(chunk, mentions):
            first, last = group[0].start, group[-1].end
            part = chunk[begin:end]
            if _foreign_club(part, first - begin, last - begin, team) or not _subject_link(part, first - begin, last - begin, nomination):
                continue
            # Treat the linked subject as the same placeholder used during training.
            phrase = chunk[begin:first] + "PLAYER" + chunk[last:end]
            # A second player joined by "and" can still be covered by the same
            # attributed introduction. Carry uncertainty across a clause boundary.
            preceding = _fold(sentence[:left + begin])
            local_title = title + (" rumour" if _RUMOUR.search(preceding) else "")
            origin = "secondary" if _REPORTED.search(preceding) else authority
            result = _judgement(phrase, origin, local_title, nomination)
            if len(group) == 1:
                # An explicit named subject can govern a coordinated second
                # predicate even if the first verb was not a publishable event.
                prior_title = local_title + (" rumour" if _RUMOUR.search(_fold(phrase)) else "")
                prior_origin = "secondary" if _REPORTED.search(_fold(phrase)) else origin
                prior = group[0].ident, left + begin, prior_origin, prior_title
            if result:
                for ident in dict.fromkeys(m.ident for m in group):
                    claims.append(_record(sentence, offset, left + begin, left + end, ident, players, result))
    return claims


def _record(sentence, offset, start, end, ident, players, judgement):
    while start < end and sentence[start].isspace():
        start += 1
    while end > start and sentence[end-1].isspace():
        end -= 1
    event, status, availability, event_score = judgement
    name, position = players[ident]
    row = {
        "player_id": ident, "player_name": name, "event": event, "status": status,
        "availability": availability, "quote": sentence[start:end],
        "start": offset + start, "end": offset + end,
        "model_version": MODEL_VERSION, "confidence": round(event_score, 6),
    }
    if position:
        row["position"] = position
    return row


def extract(text: str, roster: list[dict], *, authority: str = "secondary", title: str = "", team: str = "") -> list[dict]:
    """Identify grounded, team-roster-linked English claims, in document order.

    `official` denotes the team's own verified origin. Unrecognised origins are
    secondary. Titles can lower certainty, never supply subject or evidence.
    Pass retained article text exactly; offsets index Python Unicode characters.
    """
    return _extract(text, roster, authority, title, team)


def _extract(text, roster, authority, title, team, nomination=None, selected=None):
    if not isinstance(text, str) or not isinstance(title, str):
        raise ValueError("news text and title must be strings")
    if len(text) > 250_000:
        raise ValueError("article exceeds the extraction limit")
    if not isinstance(roster, (tuple, list)):
        raise ValueError("a team roster snapshot is required")
    if not isinstance(team, str):
        raise ValueError("team must be a club name")
    if _DIRECTIVE.search(text) or _DIRECTIVE.search(title):
        return []
    roster_index = _roster_index(roster)
    claims = []
    for start, end in _ranges(text, _PERIOD):
        sentence = text[start:end]
        if selected and (end <= selected[0] or start >= selected[1]):
            continue
        if sentence.strip() and len(sentence) <= 1200:
            claims.extend(_sentence_claims(sentence, start, roster_index, authority, _fold(title), team, nomination))
    return claims


def validate_local_claim(text, roster, proposal, *, authority="secondary", title="", team=""):
    """Ground a local language model's nomination, including original attribution.

    The model must quote the complete statement or a broader exact passage.
    Authorship, the explicit player and temporal/scope checks are independent
    of model output. An unknown availability is never promoted to a movement.
    """
    if not isinstance(proposal, dict) or not isinstance(text, str):
        return None
    start, end = proposal.get("start"), proposal.get("end")
    if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
            or end - start > 1600 or proposal.get("quote") != text[start:end]):
        return None
    event, status, availability = (proposal.get(k) for k in ("event", "status", "availability"))
    severity = {"rumour": 0, "reported": 1, "confirmed": 2}
    if event not in ("injury", "suspension", "return", "availability", "transfer"):
        return None
    if status not in (*severity, "denied") or availability not in ("out", "doubtful", "available", "unknown"):
        return None
    for row in _extract(text, roster, authority, title, team, nomination=event, selected=(start, end)):
        if row["player_id"] != proposal.get("player_id") or row["event"] != event:
            continue
        # Trim only whitespace or terminal punctuation for a verbatim sentence
        # whose closing full stop is outside a model's quote.
        clipped_left = text[row["start"]:min(start, row["end"])] if row["start"] < start else ""
        clipped_right = text[max(end, row["start"]):row["end"]] if row["end"] > end else ""
        if clipped_left.strip(" \t\r\n\"“”'‘’") or clipped_right.strip(" .!?;,:\t\r\n\"“”'‘’"):
            continue
        if (status == "denied") != (row["status"] == "denied"):
            continue
        if availability != row["availability"] and availability != "unknown":
            continue
        if status != "denied" and severity[status] < severity[row["status"]]:
            row["status"] = status
        row["availability"] = availability
        return row
    return None


if __name__ == "__main__":
    from .news_ai_benchmark import main

    main()
