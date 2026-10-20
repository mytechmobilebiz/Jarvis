"""The canonical form of a research query (app/research/canon.py): the text screened is the text sent."""

from __future__ import annotations

import pytest

from app.research import canon
from app.research.canon import QueryRefused, canonical


def test_the_canonical_form_is_nfkc_without_invisible_characters_and_with_ascii_dashes_and_spaces():
    c = canonical("  ０７８–０５‑１１２０  and​  jane‮.doe­@example.com\t")
    assert c.text == "078-05-1120 and jane.doe@example.com"


def test_the_skeleton_folds_look_alikes_accents_and_case():
    assert canon.skeleton("еquifax") == "equifax"  # Cyrillic е
    assert canon.skeleton("José GARCÍA") == "jose garcia"
    assert canonical("𝐞𝐪𝐮𝐢𝐟𝐚𝐱 ｂｒｅａｃｈ").skeleton == "equifax breach"
    assert canonical("Ѕасһа Сое").skeleton == "sacha coe"  # all Cyrillic


def test_a_query_of_128_characters_passes_and_a_longer_one_is_refused_never_cut():
    assert canonical("a" * 128).text == "a" * 128
    with pytest.raises(QueryRefused) as exc:
        canonical("a" * 129)
    assert exc.value.code == "too_long"
    assert canonical("a" * 120 + "​" * 50).text == "a" * 120  # invisible characters do not count


@pytest.mark.parametrize("raw", ["", "   ", "​‍﻿", "\t\n", None, 42])
def test_empty_or_invisible_queries_are_refused(raw):
    with pytest.raises(QueryRefused) as exc:
        canonical(raw)
    assert exc.value.code == "empty_query"


@pytest.mark.parametrize("raw", ["еquifax report", "paypal рaypal", "wοrdpress"])
def test_words_mixing_scripts_are_refused(raw):
    with pytest.raises(QueryRefused) as exc:
        canonical(raw)
    assert exc.value.code == "mixed_script"


@pytest.mark.parametrize("raw", ["call ٤٠٤ now", "४०४"])
def test_digits_that_are_not_ascii_are_refused(raw):
    with pytest.raises(QueryRefused) as exc:
        canonical(raw)
    assert exc.value.code == "non_ascii_digits"


def test_words_in_one_other_script_are_kept_as_typed():
    assert canonical("日本語のテキスト").text == "日本語のテキスト"
    assert canonical("Москва погода").text == "Москва погода"


@pytest.mark.parametrize("raw, run", [
    ("4 0 4 5 5 5 0 1 9 9", "4045550199"),
    ("404.555.0199", "4045550199"),
    ("(404) 555-0199", "4045550199"),
    ("four zero four five five five zero one nine nine", "4045550199"),
    ("4o4 is not a number but 404 five five 5 0199 is", "4045550199"),
])
def test_numbers_are_seen_whole_however_they_are_written(raw, run):
    assert run in canonical(raw).digit_runs


@pytest.mark.parametrize("raw, run", [
    ("ssn 123,45,6789", "123456789"),
    ("123 dash 45 dash 6789", "123456789"),
    ("card 4111,1111,1111,1111", "4111111111111111"),
    ("call 404, 555, 0199", "4045550199"),
    ("404;555;0199", "4045550199"),
    ("404|555|0199", "4045550199"),
    ("404:555:0199", "4045550199"),
    ("404#555*0199~", "4045550199"),
    ("404 dash 555 dash 0199", "4045550199"),
    ("404 dot 555 dot 0199", "4045550199"),
    ("404 hyphen 555 slash 0199", "4045550199"),
    ("four oh four, five five five, oh one nine nine", "4045550199"),
])
def test_commas_other_punctuation_and_separator_words_do_not_split_a_number(raw, run):
    assert canonical(raw).digit_runs == (run,)


def test_separate_numbers_stay_separate():
    assert canonical("WordPress 6.9 release notes 2026").digit_runs == ("69", "2026")
    assert canonical("dash 404 then dot 555").digit_runs == ("404", "555")  # a separator word only inside a number
    # A list of years reads as one number (and the screen refuses it as a phone number: it fails closed).
    assert canonical("2023, 2024, 2025 trends").digit_runs == ("202320242025",)


def test_words_spelled_letter_by_letter_are_joined():
    assert "equifax" in canonical("e q u i f a x breach").joined
    assert "jane" in canonical("j.a.n.e doe").joined
    assert canonical("a b testing").joined == "a b testing"  # two letters are not a spelled word


@pytest.mark.parametrize("raw", [
    "404·555·0199",  # any punctuation between the digits, not only a fixed list
    "404‧555‧0199",
    "404•555•0199",
    "404'555'0199",
    "404=555=0199",
    "404[555]0199",
    "404⁄555⁄0199",
    "4O4-555-O199",  # letters written for 0 and 1 next to a digit
    "4O4 555 Ol99",
    "(4O4) 555-0199",
    "404 555 01 ninety nine",  # tens words
    "404 five fifty-five 0199",
    "four oh four, five fifty five, oh one ninety nine",
    "404 double five five 0199",
])
def test_any_punctuation_look_alike_letters_and_tens_words_do_not_split_a_number(raw):
    assert canonical(raw).digit_runs == ("4045550199",)


def test_tens_words_and_look_alike_letters_read_as_written():
    assert canonical("twenty twenty four trends").digit_runs == ("2024",)
    assert canonical("ninety").digit_runs == ("90",)
    assert canonical("five hundred").digit_runs == ("500",)
    assert canonical("770 555 o123").digit_runs == ("7705550123",)
    assert canonical("oil prices and lol").digit_runs == ()  # the letters alone are no number


def test_ideographic_full_stops_are_dots():
    assert canonical("jdoe1987@protonmail。com").text == "jdoe1987@protonmail.com"
    assert canonical("jdoe1987@protonmail｡com").text == "jdoe1987@protonmail.com"  # NFKC makes it U+3002 first


def test_apostrophes_inside_words_can_be_dropped_and_phone_numbers_have_their_forms():
    assert canon.unapostrophe("mary-jane o'neil, d'angelo's 'quoted'") == "mary-jane oneil, dangelos 'quoted'"
    assert canon.phone_numbers("14045550199") == ("4045550199", "5550199")
    assert canon.phone_numbers("5550199") == ("5550199",)
    assert canon.phone_numbers("555019") == ()
