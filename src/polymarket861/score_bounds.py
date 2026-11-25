"""The supported full-time score domain for an individual football match."""

INVALID_SCORE = "invalid_regulation_score"


def valid_score(pair):
    return (
        isinstance(pair, (list, tuple))
        and len(pair) == 2
        and all(type(goals) is int and 0 <= goals <= 50 for goals in pair)
    )
