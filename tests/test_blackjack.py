import blackjack


def test_build_deck_has_52_unique_cards():
    deck = blackjack.build_deck()
    assert len(deck) == 52
    assert len(set(deck)) == 52


def test_hand_value_handles_aces():
    hand = [("A", "♠"), ("9", "♦")]
    assert blackjack.hand_value(hand) == 20

    hand = [("A", "♠"), ("9", "♦"), ("A", "♥")]
    assert blackjack.hand_value(hand) == 21

    hand = [("A", "♠"), ("9", "♦"), ("A", "♥"), ("K", "♣")]
    assert blackjack.hand_value(hand) == 21


def test_card_value_mappings():
    assert blackjack.card_value(("K", "♣")) == 10
    assert blackjack.card_value(("5", "♣")) == 5
    assert blackjack.card_value(("A", "♣")) == 11
