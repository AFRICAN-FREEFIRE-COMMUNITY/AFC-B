# Changelog

The AFC server's releases, newest first. AFC has ONE version number for the whole product, this
server and the AFC_Frontend website together: a release is tagged `vX.Y.Z` in every repo it changed
(this repo on its merge commit to main), and the number is counted across both (`release.json`).

Rule R89 (owner, 17 Oct 2026). Write the bullets in the PR, under `## Unreleased`, in plain words
about what a person or a partner notices. The headings decide the kind of release:

- `### Breaking` makes it a **major** (v7.4.2 to v8.0.0): something stops working for people or
  systems outside. A partner API field or address removed or renamed is Breaking (Game Evo reads
  it); so is anything that makes every user sign in again. Only on the owner's word.
- `### Added` or `### Changed` makes it a **minor** (v7.4.2 to v7.5.0): a new feature, endpoint or
  API field (additive), or a deliberate change to how a feature behaves.
- `### Fixed`, `### Security` or `### Internal` alone makes it a **patch** (v7.4.2 to v7.4.3).

At release time `node ~/.claude/skills/versioning/scripts/release.mjs next` prints the number;
rename `## Unreleased` to it, merge, then tag the merge commit. A version tag pushed with the wrong
number is blocked before the push.

## v7.29.0 - 2026-11-27

### Changed
- KOL nominations: an award where the creator picks only themselves counts as empty, so the ballot counts only once they add somebody else there (a self-pick beside others still counts). The messages and emails say so in every language, and the picks email marks such an award.

## v7.28.1 - 2026-11-27

### Fixed
- KOL nominations: the messages no longer say "You are one of the creators on Garena's KOL list" (owner: not needed).

## v7.28.0 - 2026-11-27

### Changed
- KOL nominations: the copied messages, the code emails and their subject now say plainly that this is the official nomination round of the Garena KOL Awards, Garena's own awards ceremony, not a third-party award; the email also says it is hosted on the African Freefire Community website, which sends it.

## v7.27.0 - 2026-11-27

### Changed
- Converted amounts use the Central Bank of Nigeria's official daily rates for every currency it publishes (US dollar, euro, pound, CFA franc, yen, yuan, Swiss franc, Danish krone, rand, riyal, dirham). The dollar uses the CBN's official NFEM rate of the day. All other currencies keep the rates they had.
- The rates address (auth/fx-rates/) now also says, for each currency, where its rate came from and the date the CBN published it, plus the CBN credit line to show beside converted amounts. Nothing that was there before changed.
- Rates refresh at most once an hour (was twelve hours), so a day's CBN rate, or a revision of it, shows up soon after it is published.

## v7.26.0 - 2026-11-27

### Added
- KOL nominations: every creator gives an email and a WhatsApp number on the ballot before their first pick (new address PUT polls/kol-nominations/<edition>/contact/), and admins and KOL admins can type or fix them per creator. `manage.py kol_import_contacts` loads the emails Garena's sheet already holds without overwriting any.
- KOL nominations: the codes go out by email from the admin tab, to one creator, the ticked ones or everyone with an address (new address POST polls/admin/kol-nominations/<edition>/emails/), paced for the mailbox, from info@ as African Freefire Community, in each creator's language; the tab shows sent or not sent per creator.
- KOL nominations: a creator can email themselves their picks (new address POST polls/kol-nominations/<edition>/email-me/), with no code in the email.

### Changed
- KOL nominations: a ballot counts only once every award has at least one pick. A ballot saved with an empty award (or whose only pick in an award was switched off) stays saved but leaves the picks so far, "Have nominated" and Garena's live page until it is filled. The admin rows show how many awards a started ballot fills.
- The code messages (copied and emailed) say the link and code belong to that creator alone and that anyone they share it with replaces their picks, and that every award needs at least one pick.

## v7.25.0 - 2026-11-26

### Added
- KOL nominations: a new role, KOL Admin, for Garena's KOL staff. Granted in Settings like any role, it opens the admin panel on the KOL nominations of an edition AFC has set a round up on, and nothing else: codes, spare codes, copying, switching people off, merging, the round's dates and Garena's live link. It never makes the person AFC staff (their account stays a player account), so it reaches no other poll, edition, event, dashboard or player detail. New address GET polls/admin/kol-nominations/ lists the editions that have a round.
- Garena's private live page (polls/live/<token>/) now carries the creators' nomination round: picks per candidate in each award, ballots in out of the creators who may vote. Counts only, nothing about who picked whom.

### Security
- The admin reads that let in anybody holding any role (player details with email, the admin player list, email search, dashboard counts) no longer count a partner's role as AFC staff.

## v7.24.0 - 2026-11-26

### Added
- KOL nominations: spare codes with nobody behind them, for creators found after the list was made. An admin makes up to 50 at a time; the first person to open one gives their name, TikTok @handle and country and becomes a creator in the round (a voter and a candidate), or the same person when somebody had already added them by handle. A handle already on the KOL List is refused, so a spare code never takes over another creator's ballot.
- KOL nominations: poll admins can read a live code back and copy it with its ready message (in the creator's language, or the language chosen for a spare code), and revoke one code. Codes are now also kept encrypted (their own key derived from SECRET_KEY), besides the hash the door looks them up by.
- KOL nominations: each award carries a stable key (creator, breakthrough, educational, streamer, voice, funniest) so the pages can explain it in the reader's language.

## v7.23.0 - 2026-11-26

### Added
- KOL creators' nominations: the creators on Garena's KOL List nominate for the six KOL Awards with a personal code each (no account), up to 6 per award, themselves included, and can change their picks until the round closes. A creator can also add anybody who is not on the list by TikTok @handle or profile link: that person becomes a candidate on every creator's ballot (never a voter, never named with who added them), one person counts at most once per award, and additions are capped per creator (36 in the round, 10 a minute). Admins can switch an added person off or merge a duplicate into the person it repeats (its picks move, never counted twice). Codes are stored hashed, a wrong code is limited to ten tries a connection a quarter hour (and nginx's code zone), a new code replaces the old one at once. AFC poll admins set the round's dates, load the creators (`manage.py kol_import_creators`, made from the private sheet by `tools/kol_sheet_to_json.py`, never an email), download the codes with a ready-to-send message in English, French or Portuguese, switch a creator off, and see who has voted and the picks per creator, never who picked whom.

## v7.22.0 - 2026-11-23

### Added
- Game file archive: every game file of every map now reaches the server and is kept, filed under its event, stage,
  group and map: the match result file, the debugger log, the replay (its .json and .bin), the safe zone log, the heat
  map and the small marker files custom rooms write. AFC Capture 1.7.0 sends them (only those names; the account file
  and the name file never leave the PC), the website's result upload and Game files box keep what they already
  receive, and a file that arrives before its map is scored is filed under the map the moment it is. The same file is
  stored once; a debugger log that grew during the evening replaces its shorter start. AFC admins (every event) and
  the event's organizers (their own) can list, download one file, or ask for a zip of a map, a group, a stage or the
  whole event, built in the background and kept 24 hours. Raw files are never served by their stored address. The
  archive refuses new files when the server has under 15 GB free and never slows or fails a result upload.
  The Archive tab that shows all this on the leaderboard page comes in a later release; until then the files are
  kept and reachable through the download addresses. AFC inbox #603, #606, #610, #611.

### Fixed
- A guest registration (a team with no AFC account, added from imported results) can be given a letter from its
  row on Registered Teams. Its letter picker was always greyed out, and the server only looked a team up by its
  AFC team. From the 18 Nov 2026 open list.

## v7.21.3 - 2026-11-21

### Fixed
- The live map's MVP that AFC LIVE receives now follows the whole MVP rule the organizer or admin saved
  on the leaderboard's MVPs tab, the "winning team" choice included. With "winning team" there is no
  MVP while two or more teams are still in, and once one team is left the MVP comes only from that
  team, the same way a finished map's MVP is picked. It used to look at every player in the lobby
  (AFC inbox #577).
- Each map's MVP is now stored by that same rule, so player profiles and the players list count a
  player's MVPs, the rankings score them, and the partner API's map `mvp` names the player. All three
  read 0 or empty before, because nothing stored a map's MVP. A new result gets its MVP within 10
  minutes, saving a new rule on the MVPs tab updates the event's maps at once, and every event is
  checked again at 03:45, before the rankings run at 04:00, so maps already played get theirs the
  first night (292 maps, 36 events). Rankings move for players with MVPs (AFC inbox #579).

### Internal
- Every pull request now runs the Django tests of the parts of the server it touches, in up to six parallel jobs
  (`tools/test_plan.py` picks them: a changed app plus every app whose code names it), and the whole suite runs every
  night on main and opens an issue when it is red. Main stayed red twice for two days with nobody seeing it, because
  CI ran no Django tests. The job is not a required check, so an urgent fix can still merge. The awards hall of fame
  is now listed among the public answers (it shipped in v7.17.0 without that line). AFC inbox #580, #581.

## v7.21.1 - 2026-11-19

### Fixed
- An event no longer completes early because a stage was added to it late. The stage that decides the event is
  the one marked as the finals, else the last stage in the event's running order (manual order, then start
  date), the order every page already shows; it used to be the most recently created stage, so the NATIONAL
  FREE FIRE PRO LEAGUE, whose QUARTER FINALS was added after its FINALS, read completed from 10 Nov with the
  finals still to play. The same choice decides which teams "reached the final stage", when the event's prize
  pool is attributed, and the partner API's final_stage. AFC inbox #627.

## v7.21.0 - 2026-11-19

### Changed
- A stage (and an event, through its final stage) now completes only when ALL its results are in and confirmed:
  every lobby holds its planned number of maps, each with a result (maps created one at a time no longer make it
  read completed between games); every bracket is decided by its own rule (the final, plus the 3rd-place match
  if any; a league when every match is played), so a stage with two brackets waits for both; and nothing is
  waiting for an organizer (no parked AFC Capture upload, no team-submitted map or bracket result pending
  review). AFC inbox #626.

## v7.20.5 - 2026-11-19

### Fixed
- A stage now reads "completed" as soon as every one of its maps has its result, instead of staying "ongoing"
  until the day after its end date (the Pro League semi finals read ongoing with all 18 maps scored). It uses
  the same rule that already completes an event. A stage that gets a new map without a result goes back to
  ongoing, and a stage an organizer paused is no longer switched back to ongoing by the five-minute sweep.

## v7.20.4 - 2026-11-19

### Fixed
- Uploading a map's results (AFC Capture or the website) is fast again and no longer slows down as an event goes
  on. Every upload re-saved every team result of the whole event, and each save queued a ranking recalculation
  for that team and each of its players: one map of the Pro League semi finals queued 1,112 of them (5,556
  database queries on a local copy; the server took about 20 s, which is what tripped AFC Capture's 30 s wait in
  AFC inbox #623). Now only results that changed are saved, and the map just written queues its own 12 teams
  and their players once (309 queries on the same copy). Stored results are identical.
- The Broadcast Kit's HOW_TO_USE.txt now says to delete each folder the zip carries from Free Fire_64_Data
  before copying (it names them), instead of "Overwrite when asked", which left an earlier kit's pictures
  mixed in with the new ones (AFC inbox #621). None of those folders comes with the game.

### Internal
- The esport picture tests upload a real PNG again. Their hand-typed test picture did not decode, so
  since every image upload checks the bytes (v7.17.0) 8 of them failed; real uploads were never affected.

## v7.20.2 - 2026-11-18

### Fixed
- A game is counted once per event. An upload of a game the event already holds (same Free Fire game id
  in the file name, or the same result when the file was renamed) is refused with code
  `capture_duplicate_game`, naming the map that holds it; nothing is scored, parked or created. On 18 Nov
  2026 AFC Capture gave up waiting after 30 s while the server was still scoring Day 3's first game, sent
  it again, and it became MATCH 1 and MATCH 2 (AFC inbox #623). The check runs again under a lock, so a
  retry that arrives while the first upload is still being scored is refused too.
- A website upload into the map that already holds the game still re-scores it; into another map it is
  refused. A parked ("decide later") copy of a game is retired when the game is scored, and resolving one
  of a game already in is refused with the same code.
- Clearing or deleting a scored map no longer hides the live board of the map being played (the live
  map vanished from the site and AFC LIVE for four minutes on 18 Nov 2026, inbox #622).

## v7.20.0 - 2026-11-18

### Added
- The Broadcast Kit zip now carries the in-map team flags (FlagLogos): each team's logo on an empty
  banner the organizer uploads, sized and placed as they choose. Letters A to X only: the game has no
  flag for Y and Z (AFC inbox #613 to #618).
- Team billboards (TeamBillboard) and a "Room seating.txt" sheet in a one-lobby kit: each team gets a
  room slot in letter order, and its billboard shows its logo and name on the organizer's art.
- The sky title now lands in EnvWeather with the folder that switches the effect on (wide title
  2048 x 256 or logo 2048 x 512). It used to go to PCTexture, where the game never read it.
- Panel backgrounds (HUD_PIC) and map skins (PCTexture: billboards, plane, airdrops, vehicles) can be
  added by the game's own file names; anything else is left out and listed in HOW_TO_USE.txt.
- The name under each player can be the team name, the team's country (the game shows its flag
  icon) or the player's role (role icon).
- `GET /events/<id>/broadcast-kit/` answers each team's slot, flag letter, logo address and name tag
  values, `one_room`, and the game's name lists.

### Changed
- PlayerNameOverwrite.json is written the way the last file that loaded was: Windows line endings,
  only its 18 colours (the caster row is no longer white), names cut to 12 and team or country to
  18, only characters a loaded file held (a lowercase f or w becomes its capital), at most 101 rows,
  and no extra key for a caster without a UID.

## v7.19.0 - 2026-11-16

### Added
- In-game letters are now distinct per LOBBY, not per event. Two teams may hold the same letter as
  long as they never share a lobby, so an event with more than 26 teams can be fully lettered. An
  admin or organizer chooses whether a letter applies to ALL stages (the team keeps it) or only to
  the stages they pick; a stage letter wins over the all-stages one in that stage. A round robin day
  lobby counts both of its groups (AFC inbox #602).
- `POST /events/assign-team-letters/` saves many teams' letters at once (the "Suggest letters" panel's
  Apply all). The check reads the final result, so two teams can swap letters in one save, and a
  clash anywhere saves nothing. `assign-team-letter/` takes an optional `stage_ids`.
- `GET /events/event-team-letters/` now also lists every stage's lobbies with their teams and each
  team's per-stage letters.
- The broadcast kit (client files zip) can be built for one lobby (`group_id`) and uses the letters
  of the chosen stage, so two lobbies that share letters never overwrite each other's flag files.

## v7.18.4 - 2026-11-13

### Fixed
- The group draw on a round robin stage now puts each team in ONE group, A, B or C. It used to deal
  its cards over the day lobbies ("Day 1: A+B"), so a team landed in a single day. A card now hides
  a group, and turning it over places the team in that group and in every day that group plays
  (group A plays Day 1: A+B and Day 2: A+C). The board says "You are in Group A" (AFC inbox #570).
- Saving the event's edit page no longer replaces a round robin stage's groups with new ones. It
  keeps them, so a group draw's cards survive the save, and while a draw is open the picks are not
  overwritten by the team lists the edit page loaded earlier.

## v7.18.2 - 2026-11-12

### Fixed
- The live leaderboard no longer gives placement points to teams still alive while a map is being played.
  A team scores its placement points once it is eliminated (its place is final); the last team standing
  scores first place when it is the only one left. Until then a team still alive scores its kills only
  (AFC inbox #558).

## v7.18.1 - 2026-11-12

### Fixed
- Room details and group messages now reach every inbox: the emails go out one every 2.5 seconds and an address
  the mail server refuses is tried again after 20 and 60 seconds. The LCQ room details on 12 Nov lost 31 of 55
  emails to Microsoft 365 cutting the connection for sending too fast (AFC inbox #557).

## v7.17.1 - 2026-11-12

### Fixed
- The per-group "advance to next stage" button now follows a stage's Advancement Routing rules. It used to send
  the group's top N to the next stage in order whatever the rules said: on 12 Nov the National Free Fire Pro
  League's Quarter Finals top 6 of each group went to the LCQ instead of the Semi Finals.

## v7.17.0 - 2026-11-12

### Added
- "That was the last map for now" (inbox #501): a group can be marked done for the day from the leaderboard's Stats
  screen (`POST events/<id>/groups/<group_id>/session/`); `capture/context` and `events/<id>/board/` carry the group's
  `session` (playing, done_for_now, finished, waiting) so AFC Capture and AFC LIVE know the games are over for now.
  The mark lapses by itself when a later map is played.
- A group's Stats on the leaderboard pages (`events/<id>/board/`): every team AND every player number over the whole
  group, one map, or any maps you choose: kills, damage, assists, knockdowns, knocked, headshots, deaths, revives,
  grenades, gloo walls, medkits, survival time, most used gun; plus the map being played right now. The same numbers
  download as a spreadsheet (`events/<id>/board/export/`, teams or players) (owner, 11 Nov 2026, inbox #538 #539). The
  leaderboard graphic (`events/<id>/stages/<id>/graphic/`) takes `match_ids` too: one map's board, or the maps you pick.
- Game files reads a debugger log with AFC Capture's own reader, so an uploaded log gives every number Capture would
  have sent (knocks, knocked, grenades, gloo walls, medkits, the gun, the kill feed), and it takes the map's replay
  file (`Replays/ReplayInfo_<match>_<date>.bin`) for every player's damage and the gun of every kill and knock. Each
  map lists the files still missing by their real names, and offers the placements the log itself shows when the
  result file is not there (owner, 11 Nov 2026, inbox #541).
- `events/capture/context/?standings=1` (AFC LIVE) carries `live`: the group's table with the map in progress added,
  the kill leaders with that map's kills, and that map's provisional MVP by the event's own MVP rule, so AFC LIVE
  shows the website's numbers during a map too (inbox #540).
- Admins can UPLOAD a nominee's picture in the ballot builder (`POST/DELETE polls/admin/polls/<slug>/options/<id>/image/`)
  instead of only pasting a link: the bytes are checked and re-encoded, stored under an opaque name, at most 10 MB.
- Awards editions can be HIDDEN until release (`hidden`): a hidden edition, its ballots and pages answer 404 to
  everybody but an AFC poll admin (who previews it), are left out of every list and the hall of fame, and take no
  votes. A themed (KOL Awards) edition is created hidden; switching it off releases it (owner, 11 Nov 2026, #543).
- One WhatsApp number, one ballot (`whatsapp_required` on an edition): a voter proves a number by sending
  "AFC <code>" to the AFC WhatsApp number (`polls/whatsapp/start/`, the webhook's new inbound handler) or by typing
  back a code AFC sends (`polls/whatsapp/send-code/`, `verify/`, three an hour); a number backs one account; codes are
  stored hashed. New setting `WHATSAPP_BUSINESS_NUMBER` (owner, 2 Nov 2026, #447 #449).
- `polls/hall-of-fame/`: every published winner of every released edition, newest season first, and who has won
  most (#445).
- A group's Stats on the leaderboard pages (`events/<id>/board/`): every team AND every player number over the whole
  group, one map, or any maps you choose: kills, damage, assists, knockdowns, knocked, headshots, deaths, revives,
  grenades, gloo walls, medkits, survival time, most used gun; plus the map being played right now. The same numbers
  download as a spreadsheet (`events/<id>/board/export/`, teams or players) (owner, 11 Nov 2026, inbox #538 #539). The
  leaderboard graphic (`events/<id>/stages/<id>/graphic/`) takes `match_ids` too: one map's board, or the maps you pick.
- Game files reads a debugger log with AFC Capture's own reader, so an uploaded log gives every number Capture would
  have sent (knocks, knocked, grenades, gloo walls, medkits, the gun, the kill feed), and it takes the map's replay
  file (`Replays/ReplayInfo_<match>_<date>.bin`) for every player's damage and the gun of every kill and knock. Each
  map lists the files still missing by their real names, and offers the placements the log itself shows when the
  result file is not there (owner, 11 Nov 2026, inbox #541).
- `events/capture/context/?standings=1` (AFC LIVE) carries `live`: the group's table with the map in progress added,
  the kill leaders with that map's kills, and that map's provisional MVP by the event's own MVP rule, so AFC LIVE
  shows the website's numbers during a map too (inbox #540).

### Fixed
- A debugger log dropped on a map's result upload was refused with "No team data parsed." (twice on 10 Nov). It is
  now told it is a game log and where it goes (`game_log_not_result`).
- The live leaderboard overlay showed only the map being played, with that map's points, instead of the group's
  table with the map added: a team on 133 after five maps read 22 on air. It also never went live when it followed
  a group picked on the website, and kept counting a finished map after its result was uploaded. Every reader (the
  overlays, AFC LIVE, the leaderboard page) now takes one table from the server (inbox #540).
- Players who are not on the event's roster were missing from Top Killers and every player list although their
  kills count for their team (one map: a team with 15 kills showed one player with 3). They are listed now, marked
  as not on the roster, with their damage and other numbers from AFC Capture (inbox #538).
- Once an awards edition's winners are announced, each award's published winner is named to everybody even when
  the ballot keeps its vote numbers private (the default for an award ballot). Before, the winners page said "not
  published yet" while the hall of fame named the winners. No vote count is sent with it.
- A debugger log dropped on a map's result upload was refused with "No team data parsed." (twice on 10 Nov). It is
  now told it is a game log and where it goes (`game_log_not_result`).
- The live leaderboard overlay showed only the map being played, with that map's points, instead of the group's
  table with the map added: a team on 133 after five maps read 22 on air. It also never went live when it followed
  a group picked on the website, and kept counting a finished map after its result was uploaded. Every reader (the
  overlays, AFC LIVE, the leaderboard page) now takes one table from the server (inbox #540).
- Players who are not on the event's roster were missing from Top Killers and every player list although their
  kills count for their team (one map: a team with 15 kills showed one player with 3). They are listed now, marked
  as not on the roster, with their damage and other numbers from AFC Capture (inbox #538).

### Security
- The last image doors that stored any file unchanged (profile picture, esports picture, news cover and article
  images, market screenshots) now check what the bytes really are, re-encode them and store them under an opaque
  name; a file that is not a picture is refused with not_an_image (R70, R87; inbox #531).
- No error answer carries an exception's own text any more: six OCR admin endpoints and event creation now answer a
  written sentence with a code, the details go to the log only (R79; inbox #532).

## v7.16.0 - 2026-11-10

### Added
- `events/capture/context/?standings=1` (AFC LIVE) also carries the group's kill leaders (`top_killers`): the site's
  own Top Killers board for the group (players by total kills over the group's scored maps, ties on damage then
  assists, top 10), each with name, game UID, team, kills, damage, assists and maps played. The broadcast's Kill
  Leaders now read the website's numbers, like the leaderboard and the MVP (owner, 10 Nov 2026, inbox #524).

## v7.15.0 - 2026-11-10

### Added
- `events/capture/context/?standings=1` (AFC LIVE): the group's points as the site computes them, so the broadcast
  leaderboard no longer ranks teams by the game room's own running score (on 9 Nov it put the group leader 7th):
  the points table (`scoring`), the group's official standings in the site's order (`standings`, the same table the
  leaderboard page and the partner API show), each played map's placements, kills and points (`map_points`), and the
  MVP of every map by the event's own MVP rule from the leaderboard's MVPs tab (`mvp`). Without `standings=1` the
  answer is unchanged, so AFC Capture's roster check costs what it did.

## v7.14.0 - 2026-11-09

### Added
- The capture key's event answer (`events/capture/context/`) carries a broadcast pack: per team its tag, logo address,
  country and ISO country code, per player their esport image address and in-game role. AFC LIVE's Tournament
  overlays read it to draw logos, flags and photos (owner, 4 Nov 2026, inbox #484). New keys only: AFC Capture's
  roster check reads what it read before. A logo or photo the event's media audit took off stays off.

- The event pack AFC LIVE reads (`events/capture/context/`) now carries each player's own country (`players[].country`, `players[].country_code`): the latest login country, else the profile country, the same rule as the player flags on team pages. For AFC LIVE's Player countries overlay.

### Fixed
- Damage is now stored for maps AFC Capture uploads (owner, 9 Nov 2026, inbox #512: "The site still does not store
  damage"). Capture 1.5.4 sends each player's damage as the game counts it; the server now keeps it on the player, adds
  it to the team's damage, and counts it in damage points for events that score damage. Maps uploaded earlier get their
  damage, every gun and their stats from the replay files still on the observer PC (`manage.py backfill_capture_stats`),
  without their results being rewritten. Maps from an older Capture with no replay kept stay at 0.

## v7.13.0 - 2026-11-03

### Added
- The kill stats answer (`events/kill-stats/`) carries `weapons`: kills per gun, by the name the game shows players,
  most kills first, with the player who made the most kills with each gun. Read by the new Kill stats tab on the admin
  and organizer leaderboard pages (inbox #467, approved mockup mockups/kill-stats-admin/).

### Fixed
- Gun names are the ones the game shows its players (owner, 3 Nov 2026, inbox #474: "EVO3 and SeaGull are not the
  actual names of the guns please"). They are read out of the Free Fire client's own English text, with the owner's
  permission (#475), and replace both the replay file's internal labels and the enum's developer names: 814 is the
  Skorp (was "EVO3", 75 stored rows), 812 the Hawk (was "SeaGull"), 45 the M82B (was "BLT"), 48 the Woodpecker (was
  "M21A5"), 57 the Kingfisher (was "AR15"), 21002 the M590, 635 an AWM, 809 the M7, 815 the RPK, 181 the Trogon. 253
  ids named; only 80006 of the guns played in AFC rooms is still "unknown gun (id 80006)".
- The sniper award counts the M82B (45) and every version of the owner's guns (AWM, M82B, Kar98k, M24, Hawk): the
  awakened AWM 635, the Blood Bat AWM 30924, the M24-I and M24-II, the Droid Apocalypse M82B. The table had never
  known which id the M82B was. The Hawk (812) counts too, with its Blood Bat and Storm versions (owner, 3 Nov 2026:
  "yes").
- `manage.py seed_game_weapons` renames what a machine wrote: GameWeapon rows still carrying an old generated name
  (and their sniper tick, while nobody changed it), and stored most-used weapons such as "EVO3" or "BLT". An admin's
  name or tick is never touched. Old names live in `weapon_names_retired.py`, generated by
  afc-capture/dev/build_weapons.py.
- A gun whose own name has a hyphen (MAG-7, FGL-24) is read as that gun, so stored rows the old table called "MAG7"
  are renamed too (found on the production dry run).

## v7.12.0 - 2026-11-03

### Added
- Awards vote tracking (inbox #453): every vote now records the country and region it came from (looked up in a
  local DB-IP City Lite file; the raw address is never stored), a keyed fingerprint of the connection and of the
  browser, the device type, the voter's account age, which nominee's share link brought them, the time of each
  pick and every switch between nominees, and whether a voter opened the vote sheet without finishing. The
  edition metrics answer (`polls/admin/editions/<slug>/metrics/`) carries all of it per nominee.
- Ballot review (inbox #453): flags for a profile country unlike the vote's country, three or more ballots from
  one connection, two or more from one browser, an account under three days old, and failed checks;
  `polls/admin/polls/<slug>/flags/` lists them and `polls/admin/polls/<slug>/responses/<id>/review/` clears,
  removes (a reason is required) or restores a ballot. A removed ballot leaves every tally, and who decided,
  when and why is kept.
- Garena's private live link (inbox #457, "Private link, no sign-in"): `polls/admin/editions/<slug>/live-link/`
  creates, replaces or turns off the link, and `polls/live/<token>/` answers votes per nominee with country and
  region, no voter data, places with fewer than five votes folded into Other. A wrong or turned-off link answers
  404 `live_view_not_found`.
- `manage.py refresh_geoip_db` downloads the month's DB-IP file (CC BY 4.0); the deploy fetches it when it is
  missing without failing, and a weekly task keeps it current. New dependency: maxminddb.

### Security
- Award votes stay private while voting is open (inbox #454): an award poll shows no counts during voting even
  when set to show results always, and the public poll list and poll page no longer tell anyone how many
  ballots were cast unless that viewer may see the results.
- security-rules.json: the game files upload (views_game_files.py) gets an R70 allow entry with its reason, like debugger_ingest.py: size capped, unpacked and validated by content in memory, never stored or served. The push hook had blocked on it.

### Fixed
- A player's most-used weapon reads as the gun ("Desert Eagle", "M1887") on the leaderboard editor, the
  overlays and the leaderboard graphics, not as the game's number ("9") (owner, 2 Nov: "find out what
  exact weapons they are using nd not just the codes they are showing"). AFC Capture sends the number;
  the map upload and the live push now store and serve the name from the game's own list of 252 guns,
  the same list Capture's dashboard reads (`weapons.py`, `weapon_names.py`). The most used gun in AFC
  matches, 814, is not on that list: the game's own replay files call it "EVO3" (and 812 "SeaGull"), so the
  site shows those names (research: WEBSITE/tasks/weapon-ids-and-replay-2026-11-03). A gun nobody has named
  yet reads "unknown gun (id 21002)", never a bare number, and a name an admin gives it in /admin/ wins.
- `manage.py seed_game_weapons` crashed on a table that did not have weapon 159 yet (its label was 61
  characters for a 60 character column). It now also fills every gun of the game's list (not a sniper,
  an admin's row never overwritten) and names the most-used weapons stored as a number before this
  release. Run it once after the deploy.

## v7.10.0 - 2026-11-02

### Added
- Game files (owner, 24 Oct: "upload both debugger ... and match result files simultaneously at once"):
  `POST events/<id>/game-files/read/` takes one debugger or result file at a time (gzip or plain),
  `game-files/games/` reads them as games (a game split by a client restart is joined, each game is paired
  with its result file by the game's own match id, a match is suggested, a game from before the event or
  mostly off the rosters is set aside, and the two files are compared), `game-files/result-text/` gives
  the text to upload through the normal result upload (with kills the organizer chose from the debugger,
  or, with no result file, the organizer's placements), and `game-files/debugger/` fills knocks, deaths,
  revives, headshots, survival and the kill feed.

### Fixed
- Knocks are counted from the game's knock line. The gun trace line used before also fires on direct
  kills, so knocks were counted too high (189 for 122 in one real game), in the kill feed and the
  debugger log panel.
- A knocked player who bleeds out after the knocker has left the game for good earns nobody a kill, as
  the game counts it. With this the debugger agrees with the result file for every player of 19 real
  games.

### Internal
- The event contract goldens now include the fields added since v7.4.0 (kill_feed_tab, kill_feed_export, kill_feed_overlay, team_result_claims_close_hours). Five golden tests had been failing on main since those releases because CI runs no Django tests; only additions, no key removed or renamed.

## v7.9.0 - 2026-11-02

### Added
- `GET polls/admin/editions/<slug>/metrics/` gives AFC poll admins the detailed numbers of any awards season,
  open or past: per ballot, per category, per nominee, with votes and share. It has no phase or date gate,
  so an archived season and a closed ballot answer exactly as an open one does (inbox #432).
- `GET polls/admin/legacy-ballot-metrics/` gives the same admins the original 2025 ballot still stored in the
  original awards tables: totals, votes per section, per category and nominee, and per day.

## v7.8.0 - 2026-11-02

### Added
- Awards editions can be drawn as the Detty Royale KOL Awards: a new `theme` on an edition (set in the
  admin editions form) switches `/awards/<edition>` to the themed pages. Same edition, same polls and same
  votes, only the drawing changes.
- Nominees have an address of their own (`slug` on every poll option, unique inside its award) and an
  uploaded film (`film_url`, set by an admin: MP4 or WebM, 25 MB at most, type read from the file's bytes,
  stored under an opaque name). New admin address `polls/admin/polls/<slug>/options/<id>/film/` (POST,
  DELETE). `python manage.py backfill_option_slugs` fills the slug of nominees created before this release.
- Voting one award at a time: `polls/<slug>/responses/` accepts `"merge": true`, which replaces only the
  awards named in the request and keeps the picks in the others. `polls/editions/<slug>/` now also answers
  `your_answers` (which nominee the signed-in caller picked in each award).
- A second film per nominee, the winner film (`winner_film_url`, admin address
  `polls/admin/polls/<slug>/options/<id>/winner-film/`), shown on the winner pages once winners are public.

## v7.7.0 - 2026-11-02

### Changed
- A player can now remove their esport image completely (owner, 25 Oct: "a user should be allowed to completely remove their esports image"). New `POST /auth/remove-esport-image/` (signed in, no body, no id): it deletes the caller's stored file and clears the image and its picture-check verdict; a repeat answers `esport_image_already_empty`. This reverses the replace-only rule of 12 Jun. Events the player is already in KEEP the image (one copy, kept per event, read first by the overlays, MVP, media audit, broadcast kit and media ZIP) and the player stays registered; the profile, team page, player page, polls and the partner API lose it. A new registration for an event that requires an esport image is refused with `registration_requirements_unmet` until a new image is uploaded, and uploading a new image replaces it everywhere at once, event copies included.

## v7.6.0 - 2026-11-02

### Added
- Leaderboard design columns Knocks recorded, Sniper kills recorded and Head to head record (field types `knocks_recorded`, `sniper_kills`, `h2h_record`). They are read from the stored kill events of the maps in scope, on the overlay feed rows and on the event stage graphic export, and show "-" where nothing was recorded.

## v7.5.0 - 2026-10-24

### Added
- `GET events/kill-stats/` answers what the event page's Kill stats tab shows: the event's stages, team
  and player names, in how many of a pair's fights a third team came in, and who killed and knocked who
  (every two players who met in a fight, kills and knocks each way). `?pair=<a>-<b>` gives one team
  pair's fights (stage, group, match, who started it, who came in as the third party, players left, the
  winner) with every kill and knock in order, and the two teams' players against each other; a bad
  value is `bad_pair`. The CSV gains a "who killed and knocked who" block.
- Admins and organizers can set when team result claims close (owner, 24 Oct: "admins or organizers can set
  when it closes"): the event's new `team_result_claims_close_hours`, hours after each group's start,
  empty for never (1 to 1440). After it the server refuses with `claims_closed`, and the open-maps list
  drops that group and gives `closes_at` while it is open.

### Changed
- A team can send its own map result from the moment its group starts, and still after the group has
  played (owner, 24 Oct: "and also after it has happened, just not before it has happened"). Before the
  start the server still refuses with `claims_not_open_yet`. With no closing time set (below) they
  stay open for good. `GET events/team-map-results/open/` lists every open group's maps, the latest
  group first, with `opens_at`.

## v7.4.0 - 2026-10-24

### Added
- Kill feed for the sniper award and team head to head (NFL Season 2): every kill and knock of a map is stored
  (`MatchKillEvent`, from the debugger-log upload and from AFC Capture's map upload), with the roster team of each UID. New
  `GET events/kill-stats/?event=<slug>[&stage=][&export=csv]`: "sniper kills recorded" (bolt-action weapons in
  the new `GameWeapon` table, kills with a known weapon only) and head to head (fights grouped by the owner's
  rules: a third team joins the same fight, finished kills only, 30 s of game time ends a fight). Three
  per-event switches, off by default: `kill_feed_tab`, `kill_feed_overlay`, `kill_feed_export`.

### Changed
- A team can send its own map result only while its group is playing: from the group's start
  time (in the event's time zone) until its maps are done, 30 minutes a map, plus 3 hours. Outside
  that the server refuses with `claims_not_open_yet` or `claims_closed`. Reading what the team sent
  stays open. New `GET events/team-map-results/open/?event=<slug>` lists the maps a team may claim
  right now, only from its own groups; the event page builds its picker from it.

### Fixed
- An automatic AFC Capture upload of a game that is plainly not the event's (most of its players are not on the
  event's roster, or the date in its file name is before the event) is parked in the event's pending uploads
  instead of being scored as a map. On 19 Oct a re-sent 17 Sept game was scored as event 383 Group B map 5.
  Answers 202 with `capture_parked_off_roster` / `capture_parked_older_than_event`. A slot a person picks
  (match_id, replace) is not second-guessed.

### Security
- `afc/settings.py` no longer carries a commented-out database block for the old AWS RDS host,
  which held that database's password in plain text. The database is gone (owner, 23 Oct 2026),
  so the value is dead; removing the history copy is a separate step that rewrites every branch.

### Internal
- New known-bug kind `function-must-call` and entry `be-capture-wrong-game-scored` (upload_team_match_result
  must call `_capture_park_reason`); `tests_capture_guard.py` pins the behaviour (3 of its 6 fail on main).

## v7.3.0 - 2026-10-22

### Added
- Each organizer has its own memory of how player names come out of screenshots, and it is removed
  with the organizer. Every committed or applied review teaches it: the player a reviewer kept or
  picked is remembered for that spelling, and a suggestion they turned down (changed, or marked "not
  on the platform") is remembered as wrong for it and is not picked again. The next read of that
  spelling, or a very close one, is matched from memory, only to a player on that roster (#334, #335).
- Each organizer has its own line for leaderboard screenshot reads: two maps are read at a time and
  organizers take turns, so one organizer's big batch no longer holds up another's maps. A map waiting
  in line reports its place and the expected wait; a read stuck for 10 minutes is stopped with a
  sentence and frees its place (#334).

### Changed
- The OCR name memory used to be one table for everyone, written only when an admin changed a match
  and read by exact spelling; it held nothing on production. It is now per organizer and learns from
  every review. The AI prompt only uses the event organizer's own memory.

## v7.2.0 - 2026-10-22

### Changed
- Result screenshots are now read on the server first, for free, by PaddleOCR's PP-OCRv6 reader;
  only a screenshot it is not sure about goes to the AI engine. The reader was built in June but
  its packages were never installed on the server, so every read went to Gemini (#327, #328).
  Measured on the 12 admin-checked screenshots: 98% of player rows right, every kill count it
  found right, about 3 seconds a screenshot on the server.
- An organizer with no AI key can use OCR: a sure read is used as it is, and an unsure one comes
  back as a draft marked "check every row" instead of the "connect your own AI key" refusal.
  Uploads that save results straight away, with no review step, still need a sure read or a key.
  An organization AFC switched OCR off for stays off, on-site reader included.
- A screenshot uploaded sideways or upside down is turned the right way up before it is read.

### Fixed
- Leaderboard OCR batches no longer refuse to start for an organizer whose free read was used:
  seven batches made on 24 and 26 Sept 2026 never ran.
- A reopened OCR session now says which reader read it.
- A map read from several screenshots says "check every row" when any one of them needed it, not
  only when the last one did.

### Internal
- The known-bugs check for packages the server code needs no longer exempts the OCR reader's
  packages, which is what let them stay off the server unnoticed.

## v7.1.129 - 2026-10-18

### Fixed
- A team can no longer get a 7th player by any door. An admin adding someone now meets the
  6-player limit and the one-coach / one-manager / one-analyst rule (the override only lifts the
  9-person total), and two people joining the same team at the same moment can no longer both take
  the last playing seat (#136).
- An admin "move from their current team" that the new team refuses no longer leaves the player on
  no team: every check runs before they are taken off the old one (#136).
- An SES event posted by an SNS subscription with "Raw message delivery" switched on is refused as
  `sns_raw_delivery` (it was `sns_wrong_topic`, which pointed at the wrong setting), and every
  refused SES event is written to the server log with its code (#135).

### Internal
- known-bugs `be-team-seat-unlocked` (new kind `call-outside-with`): a team member created outside
  the team's seat lock fails the checks and the deploy (#136).

## v7.1.128 - 2026-10-17

### Fixed
- The API's own coded refusals (payments, payouts, Help bot offline or timeout, OCR provider errors,
  results import, email news) reach the screen: nginx no longer swaps every API 502/503/504 for the
  maintenance page (`deploy/vps/nginx-afc.conf`, applied to the box at the ship) (#134).
- An email news test send refused by Amazon SES says why (`mail_ses_not_authorized`,
  `mail_ses_unverified`, `mail_ses_throttled`, `mail_ses_login_refused`, answered 424); "sending is
  not set up yet" is 409 (#134).

### Internal
- Releases follow rule R89: this changelog and `release.json` (one number with AFC_Frontend).
- known-bugs `be-nginx-swallows-api-errors` keeps the API block from intercepting again (#134).

## v7.1.127 - 2026-10-17

The release this server's tags start from: email news (AFC-B #132, merge 29e4bc4f) shipped with the
website's v7.1.127. The server had no tags before; its history is the merged pull requests.
