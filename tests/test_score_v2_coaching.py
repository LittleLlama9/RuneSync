from score_v2.coaching import build_coaching, build_observations


def _block(
        *, untraded=0, rapid=0, lead_windows=0, conversion_rate=0.0,
        objective_assists=0, objective_fights=0, objective_secures=0):
    return {
        "raw": {"kills": 4, "deaths": 5, "assists": 6},
        "fight_influence": {"untraded_deaths": untraded},
        "death_tempo": {"rapid_death_pairs": rapid},
        "resource_conversion": {
            "lead_windows": lead_windows,
            "conversion_rate": conversion_rate,
        },
        "objective_participation": {
            "epic_monster_assists": objective_assists,
            "grub_assists": 0,
            "epic_monster_secures": objective_secures,
            "grub_secures": 0,
            "objective_fight_involvements": objective_fights,
        },
        "vision_influence": {"available": False},
    }


def _signed(participant_id, minute, metric, sign):
    return {
        "kind": "signed_event",
        "participant_id": participant_id,
        "t_ms": minute * 60_000,
        "phase": "mid",
        "source": "match_v5",
        "metric": metric,
        "sign": sign,
    }


def test_observations_are_timestamped_facts_not_component_prose():
    observations = build_observations(
        _block(), 1,
        (
            _signed(1, 9, "objective_secure", 1),
            _signed(1, 12, "death", -1),
            _signed(2, 13, "champion_kill", 1),
        ),
        "match_v5",
    )
    assert observations == (
        "09:00 - Secured an epic objective.",
        "12:00 - Died; the timeline records the event without inferring intent.",
    )
    assert not any("component" in text.lower() for text in observations)


def test_aggregate_observation_discloses_missing_timeline_context():
    observations = build_observations(_block(), 1, (), "aggregate")
    assert observations[0] == "Post-game totals: 4/5/6."
    assert "no causal event observation is claimed" in observations[1]


def test_single_game_issue_is_withheld_until_it_repeats():
    result = build_coaching(
        _block(untraded=2), 1, (), "lcu_timeline",
        confidence=0.9, completeness=1.0,
        abstain=False, abstain_reasons=(),
        recent_comparable_features=(_block(untraded=0),),
    )
    assert result.eligible is False
    assert result.primary_focus is None
    assert any("No controllable negative pattern repeated" in reason
               for reason in result.withheld_reasons)


def test_repeated_death_pattern_generates_one_three_of_five_challenge():
    result = build_coaching(
        _block(untraded=3, rapid=1), 1,
        (_signed(1, 8, "death", -1),),
        "match_v5", confidence=0.9, completeness=1.0,
        abstain=False, abstain_reasons=(),
        recent_comparable_features=(_block(untraded=2, rapid=1),),
    )
    assert result.eligible is True
    assert result.primary_focus == "Reduce untraded deaths"
    assert len(result.challenges) == 1
    challenge = result.challenges[0]
    assert challenge["target_successes"] == 3
    assert challenge["window_games"] == 5
    assert "anti_gaming_guardrail" in challenge
    assert len(result.recurring_patterns) == 2


def test_low_confidence_and_abstention_both_explain_withholding():
    result = build_coaching(
        _block(untraded=3), 1, (), "match_v5",
        confidence=0.4, completeness=1.0,
        abstain=True, abstain_reasons=("short_game",),
        recent_comparable_features=(_block(untraded=3),),
    )
    assert result.eligible is False
    assert any("Score abstained" in reason for reason in result.withheld_reasons)
    assert any("confidence" in reason for reason in result.withheld_reasons)


def test_aggregate_never_claims_causal_coaching_even_with_recurrence():
    result = build_coaching(
        _block(untraded=3), 1, (), "aggregate",
        confidence=1.0, completeness=1.0,
        abstain=False, abstain_reasons=(),
        recent_comparable_features=(_block(untraded=3),),
    )
    assert result.eligible is False
    assert any("Aggregate evidence" in reason for reason in result.withheld_reasons)


def test_objective_contact_without_fight_can_be_a_repeated_focus():
    current = _block(objective_assists=2, objective_fights=0)
    result = build_coaching(
        current, 1, (), "lcu_timeline",
        confidence=0.9, completeness=1.0,
        abstain=False, abstain_reasons=(),
        recent_comparable_features=(current,),
    )
    assert result.eligible is True
    assert result.primary_focus == "Make objective rotations influence the contest"
    assert "Do not abandon lane" in result.challenges[0]["anti_gaming_guardrail"]


def test_direct_objective_secure_prevents_contact_without_influence_focus():
    current = _block(
        objective_assists=2, objective_fights=0, objective_secures=1,
    )
    result = build_coaching(
        current, 1, (), "lcu_timeline",
        confidence=0.9, completeness=1.0,
        abstain=False, abstain_reasons=(),
        recent_comparable_features=(current,),
    )
    assert result.eligible is False
    assert result.primary_focus is None
