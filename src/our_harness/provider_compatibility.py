"""Bounded engine upgrades that a saved-chat reconnect may explicitly review.

This is not permission to dispatch: route, account, project and saved access
must still validate and reconnect preserves the goal's execution policy.
"""

REVIEWABLE_DISPATCH_UPGRADES = {
    f"{recipe}/effective-dispatch/v1": f"{recipe}/effective-dispatch/v2-native-workspace"
    for recipe in ("claude-cli", "copilot-cli", "assistant-cli", "gemini-cli")
}


def reviewable_dispatch_contract(before: object, after: object) -> bool:
    return bool(before and after) and (
        before == after or REVIEWABLE_DISPATCH_UPGRADES.get(str(before)) == after
    )
