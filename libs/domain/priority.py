"""Identity selection priority — higher wins.

Plain integers rather than an enum so an operator can hand-set anything in between (PATCH
/identities/{id} takes a raw int); the constants just name the three points the system itself
assigns. The ordering is read by GET /identities, which is the list the attach panel renders — so
priority decides what the operator is offered first, not merely what is stored. Anything that only
gets written and never read is the "looks configured, isn't" trap this codebase keeps rediscovering.
"""

HIGH = 200      # freshly imported: what the operator most recently chose to bring in
NORMAL = 100    # the default for everything created any other way
LOW = 0         # superseded: a newer import carries the same passport, so prefer that one

#: Ordering used everywhere identities are listed for selection.
def order_key(model):
    """`(priority DESC, created_at DESC)` for a SQLAlchemy Identity model."""
    return (model.priority.desc(), model.created_at.desc())
