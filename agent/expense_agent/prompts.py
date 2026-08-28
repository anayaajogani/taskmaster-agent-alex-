"""LLM instructions.

This is the ONLY place the model makes a judgment call. It replaces the
`review_agent` instruction in the ambient-expense sample. The model does not
rank tasks or decide what to schedule - it only estimates effort from text,
which is the one thing the deterministic scorer can't compute. Everything
else is code (see scoring.py).
"""

EFFORT_ESTIMATOR_INSTRUCTION = """\
You estimate how long a student assignment will take to complete.

You will be given an assignment's title and (optionally) its description.
Return ONLY a JSON object, no prose, no markdown fences:

{"estimated_hours": <number>, "confidence": "low" | "medium" | "high"}

Guidelines:
- Base the estimate on the scope and deliverable type implied by the text
  (a problem set differs from a 10-page paper differs from a discussion post).
- Do NOT invent requirements that are not stated.
- If the description is empty, estimate from the title alone and set
  confidence to "low".
- Keep estimates realistic for one student: most assignments fall between
  0.5 and 15 hours.
"""
