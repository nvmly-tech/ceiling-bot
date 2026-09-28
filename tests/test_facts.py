"""Факты о студии в файле (facts.md): правка без программиста, подхват без перезапуска."""

import os

import pytest

from app.bot import prompts
from app.bot.assistant import LeadAssistant, parse_turn
from app.bot.facts import DEFAULT_FILE, StudioFacts
from app.services.llm import LLMRouter
from tests.test_llm import FakeProvider, turn


def write(path, text: str, bump: int = 0) -> None:
    path.write_text(text, encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + bump))  # гарантированно новое время изменения


def test_bundled_facts_are_synthetic_market_guidance():
    facts = StudioFacts(DEFAULT_FILE)
    assert "от 500 ₽/м²" in facts.text and "<!--" not in facts.text  # служебные комментарии в промпт не идут
    assert {500, 1700, 3700} <= facts.allowed_amounts
    assert "рассрочк" not in facts.text and "предоплат" not in facts.text


def test_facts_from_file_go_to_prompt_and_limit_amounts(tmp_path):
    path = tmp_path / "facts.md"
    write(path, "<!-- для студии: как править -->\n- Матовый потолок — от 610 ₽/м².\n")
    facts = StudioFacts(path)
    assert facts.text == "- Матовый потолок — от 610 ₽/м²."
    assert facts.allowed_amounts == {610}

    system = prompts.dialog_system({}, ["object"], done=False, lead_id=1, eta="завтра", facts=facts.text)
    assert "от 610 ₽/м²" in system and "от 500 ₽/м²" not in system
    assert parse_turn(turn("Матовый — от 610 ₽/м²."), allowed=facts.allowed_amounts).reply
    with pytest.raises(ValueError, match="сумма не из фактов"):
        parse_turn(turn("Матовый — от 500 ₽/м²."), allowed=facts.allowed_amounts)


def test_changes_are_picked_up_without_restart(tmp_path):
    path = tmp_path / "facts.md"
    write(path, "- Матовый — от 610 ₽/м².")
    facts = StudioFacts(path)
    write(path, "- Матовый — от 650 ₽/м².", bump=10**9)
    assert "650" in facts.text and facts.allowed_amounts == {650}


def test_broken_file_keeps_last_good_facts(tmp_path, caplog):
    path = tmp_path / "facts.md"
    write(path, "- Матовый — от 610 ₽/м².")
    facts = StudioFacts(path)
    write(path, "<!-- только комментарий -->\n", bump=10**9)  # по ошибке стёрли всё
    assert "610" in facts.text
    path.unlink()
    assert "610" in facts.text
    assert "Факты о студии" in caplog.text


def test_missing_file_at_start_falls_back_to_bundled(tmp_path, caplog):
    facts = StudioFacts(tmp_path / "нет.md")
    assert "от 500 ₽/м²" in facts.text
    assert "работаю на образце" in caplog.text and "прежней версии" not in caplog.text


async def test_assistant_uses_its_facts_file(tmp_path):
    path = tmp_path / "facts.md"
    write(path, "- Матовый — от 610 ₽/м².")
    ds = FakeProvider("deepseek", turn("Матовый — от 610 ₽/м², точнее — на замере."))
    assistant = LeadAssistant(LLMRouter([ds]), StudioFacts(path))
    from app.db import Lead

    lead = Lead(id=1, tg_user_id=1, chat_id=1, name="А", username=None, object=None, area_m2=None, area_text=None,
                ceiling_type=None, phone=None, measure_time=None, status="new", is_night=False, trello_card_id=None,
                created_at="", updated_at="", completed_at=None)
    result = await assistant.dialog_turn(lead, [], "сколько стоит?", done=False, eta="завтра")
    assert result.reply.startswith("Матовый — от 610") and "от 610 ₽/м²" in ds.calls[0][0]["content"]
