"""DAEMON Score v2 corpus, adversarial case library, and blinded review tooling.

This package is corpus/manifest/review infrastructure only. It does not
extract features, train, or route any model, and it does not touch Riot
credentials -- see ``docs/CORPUS_AND_REVIEW.md`` for the full design and the
privacy/consent policy this package enforces.

Evidence tiers (see the vault decision "Promote LCU post-game timelines into
DAEMON Score v2 evidence hierarchy"): Match-V5 full timeline > LCU post-game
timeline > Live Client capture > aggregate LCU fallback. Bulk Match-V5
authorization is separately blocked pending a Riot production key; this
package stays honest about that and remains fully usable with local LCU
evidence in the meantime.
"""
