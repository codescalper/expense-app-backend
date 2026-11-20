"""Email templates for family invitations (sent through the ``send_email`` job outbox)."""

from __future__ import annotations

from urllib.parse import quote

from app.services.email import OutgoingEmail

# The mobile app's URL scheme (mobile/app.json "scheme").
INVITE_DEEP_LINK_PREFIX = "familybudget://invite?token="

_ROLE_LABELS = {
    "admin": "an admin",
    "member": "a member",
    "viewer": "a viewer (read-only)",
}


def invitation_deep_link(token: str) -> str:
    return INVITE_DEEP_LINK_PREFIX + quote(token, safe="")


def family_invitation_email(
    *,
    to: str,
    inviter_name: str,
    family_name: str,
    role: str,
    token: str,
    expires_days: int,
) -> OutgoingEmail:
    role_label = _ROLE_LABELS.get(role, role)
    return OutgoingEmail(
        to=to,
        subject=f"{inviter_name} invited you to join {family_name} on Family Budget",
        body=(
            "Hi,\n\n"
            f'{inviter_name} invited you to join the family "{family_name}" on Family Budget '
            f"as {role_label}.\n\n"
            "Open this link on your phone to accept or decline:\n"
            f"{invitation_deep_link(token)}\n\n"
            "Or open the Family Budget app, go to Families > Join with invite code and paste:\n"
            f"{token}\n\n"
            f"The invitation expires in {expires_days} days and can be used once. If you sign in "
            "with this email address you will also see it under your pending invitations.\n"
            "If you don't know this person, you can ignore this email.\n"
        ),
    )
