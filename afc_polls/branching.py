"""
afc_polls.branching - which questions a given set of answers actually puts in front of somebody.

WHAT THIS IS FOR
    "Choosing an option leads to a different set of questions." A poll carries an ordered list of
    PollBranchRule rows, each of which watches ONE answer and shows or hides ONE question or
    section. This module turns (poll, answers) into the canonical list of question ids that are on
    that person's path, and nothing else.

THE RULE THAT MAKES IT SAFE
    Evaluation happens TWICE and the second time is the one that counts. The client evaluates
    live so the form reacts as you answer; the server calls `canonical_path` at submit and then
    DISCARDS every answer to a question that is not on it. Without that second pass, somebody who
    answers Q3, goes back and changes their mind on Q1, and submits would silently contribute a Q3
    answer they were never supposed to be asked, and the Q3 totals would be wrong in a way nobody
    would ever notice, because every individual response would look perfectly reasonable.

DEFAULT VISIBLE, NOT DEFAULT HIDDEN
    A question that no rule TARGETS is always shown. That is what makes a poll with zero rules
    simply linear, which is the common case, and it means an admin cannot accidentally hide a
    question by writing a rule about a different one. A question that IS targeted starts from the
    action of its rules: if any rule targeting it is a `show`, it is hidden until one of those
    rules is satisfied; a `hide` rule removes it once satisfied. `hide` wins over `show` when both
    are satisfied on the same question, because a rule that says "do not ask this person" is a
    stronger statement than one that says "you may".

WHAT IT DELIBERATELY CANNOT DO
    Loop back to an earlier question, or merge two divergent paths and re-diverge them. Both are
    graph shapes, and the flat rule list was chosen over a graph on purpose (see
    afc_polls.models.PollBranchRule). Neither has come up in anything the owner described.

HOW THIS CONNECTS
    Reads afc_polls.PollBranchRule / PollQuestion / PollSection. Called by afc_polls.views
    (poll_detail sends the rules to the client so the form can react, submit_response calls
    canonical_path before writing) and mirrored in
    frontend/app/(user)/polls/[slug]/_components/useBranching.ts, which implements the SAME rules
    for the live form. The two must agree; the tests in afc_polls/tests.py fix the server side of
    that agreement.
"""
from .models import PollBranchRule, PollQuestion


def _answer_option_ids(answers, question_id):
    """The option ids picked for one question, as a set. `answers` is {question_id: [option_id]},
    which is the shape both the submit path and the stored sheet use."""
    return set(answers.get(question_id) or [])


def _rating_value(values, question_id):
    """The numeric answer to a rating question, or None. Kept separate from the option ids because
    a rating is a scale point rather than a choice, and a rule comparing `gte 3` against an option
    id would be meaningless."""
    raw = (values or {}).get(question_id)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def rule_is_satisfied(rule, answers, ratings=None):
    """Does this person's answer to `rule.when_question` satisfy `rule`?

    Unsatisfied is the answer for anything unanswered, on every operator including `is_not`. That
    is not an oversight: "you did not answer Q1" is not the same claim as "your answer to Q1 was
    not X", and treating a blank as satisfying `is_not` would show a follow-up question to
    everybody who simply had not got there yet, which on a long ballot is everybody.
    """
    watched = rule.when_question_id
    value = rule.value or {}

    if rule.operator in (PollBranchRule.GTE, PollBranchRule.LTE):
        given = _rating_value(ratings, watched)
        if given is None:
            return False
        try:
            threshold = int(value.get("rating"))
        except (TypeError, ValueError):
            return False
        return given >= threshold if rule.operator == PollBranchRule.GTE else given <= threshold

    picked = _answer_option_ids(answers, watched)
    if not picked:
        return False
    wanted = set(value.get("option_ids") or [])
    if not wanted:
        return False

    if rule.operator == PollBranchRule.IS:
        # "is" on a multiple-choice question means the wanted option is among the picks. Requiring
        # an exact set match would make a rule on a "pick up to three" question almost impossible
        # to satisfy, which is not what an admin writing "when they picked Support" means.
        return bool(picked & wanted)
    if rule.operator == PollBranchRule.IS_ANY_OF:
        return bool(picked & wanted)
    if rule.operator == PollBranchRule.IS_NOT:
        return not (picked & wanted)
    return False


def canonical_path(poll, answers, ratings=None, questions=None, rules=None):
    """The question ids that are ON this person's path, in poll order.

    `answers` is {question_id: [option_id]} and `ratings` is {question_id: int}. Both may be
    partial: a half-filled form is the normal case while somebody is still answering, and this is
    what the live form asks for on every keystroke.

    `questions` and `rules` may be passed in when the caller already has them, so a submit does not
    re-query what it just read. Nothing here writes.
    """
    questions = list(questions if questions is not None else poll.questions.all())
    rules = list(rules if rules is not None else poll.branch_rules.all())
    if not rules:
        # The common case, and worth short-circuiting: a poll with no rules is linear, and asking
        # the loop below to prove that for every question is pure cost.
        return [question.question_id for question in questions]

    # Which questions and sections are TARGETED at all. Anything absent from these is always shown,
    # which is the "default visible" rule from the module header.
    targeted_questions = {rule.target_question_id for rule in rules if rule.target_question_id}
    targeted_sections = {rule.target_section_id for rule in rules if rule.target_section_id}

    shown, hidden = set(), set()
    for rule in rules:
        if not rule_is_satisfied(rule, answers, ratings):
            continue
        bucket = shown if rule.action == PollBranchRule.SHOW else hidden
        if rule.target_question_id:
            bucket.add(("question", rule.target_question_id))
        if rule.target_section_id:
            bucket.add(("section", rule.target_section_id))

    path = []
    for question in questions:
        question_targeted = question.question_id in targeted_questions
        section_targeted = question.section_id in targeted_sections if question.section_id else False
        if not question_targeted and not section_targeted:
            path.append(question.question_id)
            continue

        # `hide` beats `show`: "do not ask this person" is a stronger statement than "you may".
        if ("question", question.question_id) in hidden:
            continue
        if question.section_id and ("section", question.section_id) in hidden:
            continue

        # A targeted question is hidden until one of the rules that names it is satisfied. Being
        # targeted only by `hide` rules means it is visible until one of them fires, which is why
        # the two are tested separately rather than as one flag.
        satisfied_show = (
            ("question", question.question_id) in shown
            or (question.section_id and ("section", question.section_id) in shown)
        )
        only_hide_rules = not any(
            rule.action == PollBranchRule.SHOW
            and (
                rule.target_question_id == question.question_id
                or (question.section_id and rule.target_section_id == question.section_id)
            )
            for rule in rules
        )
        if satisfied_show or only_hide_rules:
            path.append(question.question_id)

    return path


def serialize_rules(poll, rules=None):
    """The rules, in the shape the live form reads.

    Sent to the CLIENT on purpose. A branching form that has to ask the server which question comes
    next would put a network round trip between a tap and the next question appearing, on a
    connection that is often a phone on mobile data. The client evaluating them is a UX decision
    and never a security one: the server recomputes the path at submit and discards anything off
    it, so a client that lies about its own path only removes its own answers.
    """
    return [
        {
            "rule_id": rule.rule_id,
            "order": rule.order,
            "when_question_id": rule.when_question_id,
            "operator": rule.operator,
            "value": rule.value or {},
            "action": rule.action,
            "target_question_id": rule.target_question_id,
            "target_section_id": rule.target_section_id,
        }
        for rule in (rules if rules is not None else poll.branch_rules.all())
    ]


def rating_map(answers_queryset):
    """{question_id: rating} out of a stored answer set, for re-evaluating a saved response.

    A rating lives in PollAnswer.value as {"rating": n} with a null option, which is the same shape
    the free-text types use. Pulled out here so the branch evaluator never has to know how an
    answer is stored.
    """
    ratings = {}
    for answer in answers_queryset:
        if answer.option_id is None and isinstance(answer.value, dict):
            value = answer.value.get("rating")
            if value is not None:
                try:
                    ratings[answer.question_id] = int(value)
                except (TypeError, ValueError):
                    continue
    return ratings


def branching_questions(poll):
    """The questions a rule may legally WATCH: single choice, multiple choice and rating only.

    An option id and a scale number are stable things to write a rule against. Free text is not,
    and a rule like "if the answer contains 'double'" is a bug waiting to happen, so the builder
    does not offer it. Exposed here rather than hard-coded in the builder so the two cannot drift.
    """
    return [
        question for question in poll.questions.all()
        if question.answer_type in (
            PollQuestion.SINGLE_CHOICE, PollQuestion.MULTIPLE_CHOICE, PollQuestion.RATING,
        )
    ]
