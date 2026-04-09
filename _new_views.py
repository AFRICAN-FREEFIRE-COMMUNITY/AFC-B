

# ─────────────────────────────────────────────────────────────────────────────
# DIRECT TRIAL INVITES  (Team → Player from a PLAYER_AVAILABLE post)
# ─────────────────────────────────────────────────────────────────────────────

@api_view(["POST"])
def invite_player_to_trial(request):
    """
    Team invites a player who posted a PLAYER_AVAILABLE post.
    - Caller must be team owner, manager, or coach
    - Team must have < 4 active (TRIAL_ONGOING) trials
    - No duplicate pending invite allowed
    """
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    post_id = request.data.get("post_id")
    invite_message = request.data.get("message", "")

    try:
        post = RecruitmentPost.objects.get(id=post_id)
    except RecruitmentPost.DoesNotExist:
        return Response({"message": "Post not found."}, status=404)

    if post.post_type != "PLAYER_AVAILABLE":
        return Response({"message": "This post is not a player availability post."}, status=400)

    # Resolve which team this user represents
    team = None
    if Team.objects.filter(team_owner=user).exists():
        team = Team.objects.get(team_owner=user)
    else:
        membership = TeamMembers.objects.filter(
            member=user, management_role__in=['manager', 'coach']
        ).select_related('team').first()
        if membership:
            team = membership.team

    if not team:
        return Response({"message": "You must be a team owner, manager, or coach to send a trial invite."}, status=403)

    if TeamMembers.objects.filter(team=team, member=post.player).exists():
        return Response({"message": "This player is already in your team."}, status=400)

    if DirectTrialInvite.objects.filter(team=team, player_post=post, status="PENDING").exists():
        return Response({"message": "You have already sent a pending trial invite to this player."}, status=400)

    active_team_trials = RecruitmentApplication.objects.filter(team=team, status="TRIAL_ONGOING").count()
    if active_team_trials >= 4:
        return Response({"message": "Your team already has 4 active trials. Finalize an existing trial before starting more."}, status=400)

    invite = DirectTrialInvite.objects.create(
        team=team,
        player=post.player,
        player_post=post,
        message=invite_message,
        expires_at=timezone.now() + timedelta(hours=72),
    )

    Notifications.objects.create(
        user=post.player,
        message=f"{team.team_name} has sent you a trial invite!"
    )

    player = post.player
    email_subject = f"Trial Invite from {team.team_name}"
    message_row = (
        f'<tr><td style="padding:0 24px 20px 24px;border-top:1px solid #333;">'
        f'<p style="margin:12px 0 4px 0;font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#666;">Message</p>'
        f'<p style="margin:0;font-size:14px;color:#bbbbbb;line-height:1.6;font-style:italic;">{invite_message}</p>'
        f'</td></tr>'
    ) if invite_message else ""

    email_body = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width, initial-scale=1.0"/></head>
<body style="margin:0;padding:0;background-color:#0f0f0f;font-family:'Segoe UI',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#0f0f0f;padding:40px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="background-color:#1a1a1a;border-radius:12px;overflow:hidden;border:1px solid #2a2a2a;max-width:600px;width:100%;">
        <tr><td style="background:linear-gradient(135deg,#ff6b00,#ff9500);padding:32px 40px;text-align:center;">
          <p style="margin:0 0 6px 0;font-size:11px;letter-spacing:3px;color:rgba(255,255,255,0.75);text-transform:uppercase;">African Free Fire Community</p>
          <h1 style="margin:0;font-size:26px;font-weight:700;color:#ffffff;">A Team Wants You!</h1>
        </td></tr>
        <tr><td style="padding:36px 40px;">
          <p style="margin:0 0 24px 0;font-size:15px;color:#cccccc;line-height:1.6;">
            Hey <strong style="color:#ffffff;">{player.username}</strong> &mdash;
            <strong style="color:#ff7a00;">{team.team_name}</strong> saw your availability post and wants you on their roster for a trial!
          </p>
          <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#242424;border-radius:10px;border:1px solid #333;margin-bottom:24px;">
            <tr><td style="padding:20px 24px;">
              <p style="margin:0 0 4px 0;font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#666;">Team Inviting You</p>
              <p style="margin:0;font-size:22px;font-weight:700;color:#ffffff;">{team.team_name}</p>
            </td></tr>
            {message_row}
          </table>
          <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:28px;">
            <tr><td style="background-color:#2a1a00;border:1px solid #ff6b0044;border-radius:8px;padding:16px 20px;">
              <table cellpadding="0" cellspacing="0"><tr>
                <td style="padding-right:12px;font-size:22px;">&#9201;</td>
                <td>
                  <p style="margin:0;font-size:13px;font-weight:700;color:#ff9500;text-transform:uppercase;letter-spacing:1px;">72-Hour Window</p>
                  <p style="margin:4px 0 0 0;font-size:13px;color:#cc8800;line-height:1.5;">You must accept or decline within <strong>72 hours</strong>. After that, the invite expires.</p>
                </td>
              </tr></table>
            </td></tr>
          </table>
          <table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
            <a href="https://africanfreefirecommunity.com/my-invites"
               style="display:inline-block;background:linear-gradient(135deg,#ff6b00,#ff9500);color:#ffffff;text-decoration:none;font-size:14px;font-weight:700;letter-spacing:1px;padding:14px 36px;border-radius:6px;text-transform:uppercase;">
              View &amp; Respond to Invite
            </a>
          </td></tr></table>
        </td></tr>
        <tr><td style="background-color:#141414;padding:20px 40px;text-align:center;border-top:1px solid #2a2a2a;">
          <p style="margin:0;font-size:12px;color:#555555;">This invite was sent because you have an active availability post on the AFC Player Market.</p>
          <p style="margin:6px 0 0 0;font-size:12px;color:#555555;">&copy; 2026 African Free Fire Community. All rights reserved.</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
    send_email(player.email, email_subject, email_body)
    return Response({"message": "Trial invite sent.", "invite_id": invite.id}, status=201)


@api_view(["GET"])
def view_my_trial_invites(request):
    """Player views all direct trial invites received from teams."""
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    invites = DirectTrialInvite.objects.filter(player=user).select_related(
        "team", "player_post"
    ).order_by("-created_at")

    data = []
    for invite in invites:
        if invite.status == "PENDING" and invite.expires_at < timezone.now():
            invite.status = "EXPIRED"
            invite.save(update_fields=["status"])

        data.append({
            "invite_id": invite.id,
            "team": invite.team.team_name,
            "team_id": invite.team.team_id,
            "team_logo": invite.team.team_logo.url if invite.team.team_logo else None,
            "message": invite.message,
            "status": invite.status,
            "post_id": invite.player_post.id,
            "expires_at": invite.expires_at,
            "created_at": invite.created_at,
        })

    return Response(data, status=200)


@api_view(["POST"])
def respond_to_direct_trial_invite(request):
    """
    Player accepts or declines a DirectTrialInvite.
    ACCEPT:  player < 2 active trials, team < 4 active trials → creates RecruitmentApplication + TrialChat
    DECLINE: marks invite rejected, notifies team
    """
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return Response({"message": "Invalid token."}, status=400)

    user = validate_token(auth.split(" ")[1])
    if not user:
        return Response({"message": "Invalid session."}, status=401)

    invite_id = request.data.get("invite_id")
    action = request.data.get("action")  # ACCEPT or DECLINE

    try:
        invite = DirectTrialInvite.objects.select_related("team", "player", "player_post").get(id=invite_id)
    except DirectTrialInvite.DoesNotExist:
        return Response({"message": "Invite not found."}, status=404)

    if invite.player != user:
        return Response({"message": "Unauthorized."}, status=403)

    if invite.status != "PENDING":
        return Response({"message": f"This invite has already been {invite.status.lower()}."}, status=400)

    if invite.expires_at < timezone.now():
        invite.status = "EXPIRED"
        invite.save(update_fields=["status"])
        return Response({"message": "This invite has expired."}, status=400)

    if action == "DECLINE":
        invite.status = "REJECTED"
        invite.save()
        Notifications.objects.create(
            user=invite.team.team_owner,
            message=f"{user.username} has declined your trial invite."
        )
        return Response({"message": "Invite declined."}, status=200)

    elif action == "ACCEPT":
        player_active_trials = RecruitmentApplication.objects.filter(
            player=user, status="TRIAL_ONGOING"
        ).count()
        if player_active_trials >= 2:
            return Response({
                "message": f"You are already in {player_active_trials} active trial(s). You cannot be in more than 2 at a time."
            }, status=400)

        team_active_trials = RecruitmentApplication.objects.filter(
            team=invite.team, status="TRIAL_ONGOING"
        ).count()
        if team_active_trials >= 4:
            return Response({
                "message": f"{invite.team.team_name} already has 4 active trials and cannot start more right now."
            }, status=400)

        invite.status = "ACCEPTED"
        invite.save()

        # Unified: RecruitmentApplication + TrialChat so all existing chat logic works
        application = RecruitmentApplication.objects.create(
            player=user,
            recruitment_post=invite.player_post,
            team=invite.team,
            status="TRIAL_ONGOING",
            contact_unlocked=True,
        )
        chat = TrialChat.objects.create(application=application)

        Notifications.objects.create(
            user=invite.team.team_owner,
            message=f"{user.username} accepted your trial invite. A trial chat has been created."
        )

        team = invite.team
        team_owner_email = team.team_owner.email
        team_captain_email = team.team_captain.email if team.team_captain else None
        manager_emails = list(team.memberships.filter(management_role="manager").values_list("member__email", flat=True))
        coach_emails = list(team.memberships.filter(management_role="coach").values_list("member__email", flat=True))
        recipient_emails = set(filter(None, [team_owner_email, team_captain_email] + manager_emails + coach_emails))

        team_email_subject = f"{user.username} accepted your trial invite!"
        team_email_body = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width, initial-scale=1.0"/></head>
<body style="margin:0;padding:0;background-color:#0f0f0f;font-family:'Segoe UI',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#0f0f0f;padding:40px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="background-color:#1a1a1a;border-radius:12px;overflow:hidden;border:1px solid #2a2a2a;max-width:600px;width:100%;">
        <tr><td style="background:linear-gradient(135deg,#ff6b00,#ff9500);padding:32px 40px;text-align:center;">
          <p style="margin:0 0 6px 0;font-size:11px;letter-spacing:3px;color:rgba(255,255,255,0.75);text-transform:uppercase;">African Free Fire Community</p>
          <h1 style="margin:0;font-size:26px;font-weight:700;color:#ffffff;">Trial Accepted!</h1>
        </td></tr>
        <tr><td style="padding:36px 40px;">
          <p style="margin:0 0 24px 0;font-size:15px;color:#cccccc;line-height:1.6;">Hi <strong style="color:#ffffff;">{team.team_name}</strong> Management,</p>
          <p style="margin:0 0 24px 0;font-size:15px;color:#aaaaaa;line-height:1.7;">
            <strong style="color:#ff7a00;">{user.username}</strong> has accepted your trial invite. A dedicated trial chat is now open.
          </p>
          <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#242424;border-radius:10px;border:1px solid #333;margin-bottom:28px;">
            <tr><td style="padding:20px 24px;">
              <p style="margin:0 0 4px 0;font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#666;">Player on Trial</p>
              <p style="margin:0;font-size:22px;font-weight:700;color:#ffffff;">{user.username}</p>
            </td></tr>
          </table>
          <table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
            <a href="https://africanfreefirecommunity.com/team/trials"
               style="display:inline-block;background:linear-gradient(135deg,#ff6b00,#ff9500);color:#ffffff;text-decoration:none;font-size:14px;font-weight:700;letter-spacing:1px;padding:14px 36px;border-radius:6px;text-transform:uppercase;">
              Open Trial Chat
            </a>
          </td></tr></table>
        </td></tr>
        <tr><td style="background-color:#141414;padding:20px 40px;text-align:center;border-top:1px solid #2a2a2a;">
          <p style="margin:0;font-size:12px;color:#555555;">You received this because you are a staff member of <strong style="color:#777;">{team.team_name}</strong>.</p>
          <p style="margin:6px 0 0 0;font-size:12px;color:#555555;">&copy; 2026 African Free Fire Community. All rights reserved.</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
        for email in recipient_emails:
            send_email(email, team_email_subject, team_email_body)

        return Response({"message": "Trial accepted.", "chat_id": chat.id}, status=200)

    else:
        return Response({"message": "Invalid action. Use ACCEPT or DECLINE."}, status=400)
