"""Регресс: КНБ играет по классическим правилам (🪨>✂️, ✂️>📄, 📄>🪨),
а не «угадыванием»; формулировка результата соответствует исходу."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.handlers.games import RPS_BEATS, RPS_EMOJI, rps_outcome


def test_beats_table_matches_classic_rules():
    assert RPS_BEATS["rock"] == "scissors"      # камень бьёт ножницы
    assert RPS_BEATS["scissors"] == "paper"     # ножницы режут бумагу
    assert RPS_BEATS["paper"] == "rock"         # бумага накрывает камень


def test_all_nine_outcomes():
    cases = {
        ("rock", "rock"): "draw", ("scissors", "scissors"): "draw", ("paper", "paper"): "draw",
        ("rock", "scissors"): "win", ("scissors", "paper"): "win", ("paper", "rock"): "win",
        ("scissors", "rock"): "lose", ("paper", "scissors"): "lose", ("rock", "paper"): "lose",
    }
    for (mine, theirs), expected in cases.items():
        assert rps_outcome(mine, theirs) == expected, (mine, theirs)


def test_paper_vs_scissors_is_loss_for_player():
    # именно тот кейс из жалобы: игрок 📄, питомец ✂️ -> выиграл питомец
    assert rps_outcome("paper", "scissors") == "lose"
    # и наоборот: ✂️ игрока против 📄 питомца -> выиграл игрок
    assert rps_outcome("scissors", "paper") == "win"


def test_emoji_mapping():
    assert RPS_EMOJI == {"rock": "🪨", "scissors": "✂️", "paper": "📄"}
