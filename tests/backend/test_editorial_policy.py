from backend.editorial import build_pop_liner, research_allowed


def test_pop_liners_are_localized_safe_and_rotate():
    first = build_pop_liner("en", "daytime", "Levitating", "Dua Lipa")
    second = build_pop_liner(
        "en",
        "daytime",
        "Levitating",
        "Dua Lipa",
        recent_template_ids=(first.template_id,),
    )
    french = build_pop_liner("fr", "daytime", "Levitating", "Dua Lipa")

    assert "Radio TED U" in first.text
    assert "Levitating" in first.text and "Dua Lipa" in first.text
    assert second.template_id != first.template_id
    assert "Vous écoutez Radio TED U" in french.text


def test_only_jazz_and_classical_allow_research():
    assert research_allowed("jazz") is True
    assert research_allowed("Classical") is True
    assert research_allowed("pop") is False
    assert research_allowed("classic rock") is False
    assert research_allowed(None) is False

