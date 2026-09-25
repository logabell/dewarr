from app.domain.selection_feedback import no_release_message, rejection_reasons


def test_no_results_is_distinct_from_rejected_results():
    assert "No releases were found" in no_release_message({"decisions": []})


def test_inspected_candidate_reasons_take_priority_over_unrelated_results():
    payload = {
        "decisions": [
            {"reasons": ["Wrong medium"]},
            {"reasons": ["Wrong medium"]},
            {"inspected": True, "reasons": ["No ready torrent download route"]},
        ]
    }
    assert rejection_reasons(payload) == ["No ready torrent download route"]
    assert "No ready torrent download route" in no_release_message(payload)
    assert "inspection budget" not in no_release_message(payload)


def test_reasons_are_bounded_and_deduplicated():
    payload = {"decisions": [{"reasons": ["Author missing", "Author missing", "Language"]}]}
    assert rejection_reasons(payload) == ["Author missing", "Language"]
