"""Prospective exclusions for new football paper selections."""

from decimal import Decimal


def prospective_hold(terms):
    """Use authenticated token terms, shared by discovery and manual opening."""
    if terms["family"] == "total" and Decimal(terms["line"]) == Decimal("2.5"):
        return "full_game_total_2.5"
    if terms["family"] == "result" and terms["selection"] == "home" and terms["side"] == "no":
        return "home_no"
    return None


HELD_DIRECTIONS = ("full_game_total_2.5", "home_no")
