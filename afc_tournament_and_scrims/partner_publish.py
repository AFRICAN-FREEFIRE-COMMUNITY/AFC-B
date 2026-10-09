"""
afc_tournament_and_scrims/partner_publish.py - publish an event to the Partner Data API when it
finishes (owner 2026-10-09, inbox #201).

WHY: a partner reads an event only when it is published to partners (Event.partner_published,
the gate afc_partner_api/scope.py applies first) AND one of the partner's grants covers it. Until
now publishing was a button an admin pressed per event, on the partner's page, so finished events
quietly stayed invisible to every partner. The owner asked for finished events to publish
themselves. Grants still decide WHICH partner sees an event, so publishing exposes nothing to a
partner that was not granted it.

CALLED BY (the two ways an event becomes completed):
  * views.complete_event_core, the one completion chokepoint: the date sweep (tasks.py and
    update_event_and_stage_statuses), results-based auto-complete, and the "Mark complete" button.
  * views.edit_event, when an admin or organizer moves the status to completed in the edit form
    (that path writes event_status through the event contract and never calls the core).

ONLY ON THE TRANSITION. complete_event_core returns early for an event that is already completed,
and edit_event calls this only when the status CHANGED to completed, so an admin who withdraws a
finished event is not overruled on the next save. Reopen and complete it again and it publishes
again, which is the moment its results changed.

Lives here rather than in afc_partner_api because partner_published is a column on Event, and
afc_partner_api imports this app, never the other way round (see afc_partner_api/views_admin.py
publish_event).
"""


def publish_on_completion(event):
    """Mark ``event`` published to partners. Returns True if it changed anything.

    A draft is never published: it is not a real event yet. An event already published is left
    alone. update() rather than save(), so it touches one column and cannot race a concurrent
    save of other fields (complete_event_core has just saved event_status with update_fields).
    """
    from .models import Event

    if event.is_draft or event.partner_published:
        return False
    Event.objects.filter(pk=event.pk).update(partner_published=True)
    event.partner_published = True
    return True
