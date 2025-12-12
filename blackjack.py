import random
from typing import List, Tuple

Card = Tuple[str, str]

SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]


def build_deck() -> List[Card]:
    """Create and shuffle a standard 52-card deck."""
    deck = [(rank, suit) for suit in SUITS for rank in RANKS]
    random.shuffle(deck)
    return deck


def card_value(card: Card) -> int:
    """Return the blackjack value for a single card."""
    rank, _ = card
    if rank == "A":
        return 11
    if rank in {"K", "Q", "J"}:
        return 10
    return int(rank)


def hand_value(hand: List[Card]) -> int:
    """Compute the optimal blackjack hand value counting aces as 11 or 1."""
    total = sum(card_value(card) for card in hand)
    aces = sum(1 for rank, _ in hand if rank == "A")

    while total > 21 and aces:
        total -= 10
        aces -= 1

    return total


def hand_to_string(hand: List[Card]) -> str:
    return " ".join(f"{rank}{suit}" for rank, suit in hand)


def deal_card(deck: List[Card]) -> Card:
    if not deck:
        raise ValueError("The deck is empty. Start a new game.")
    return deck.pop()


def play_round() -> None:
    deck = build_deck()
    player_hand: List[Card] = []
    dealer_hand: List[Card] = []

    # Initial deal
    for _ in range(2):
        player_hand.append(deal_card(deck))
        dealer_hand.append(deal_card(deck))

    while True:
        print("\nYour hand:", hand_to_string(player_hand), f"(value: {hand_value(player_hand)})")
        print("Dealer shows:", f"{dealer_hand[0][0]}{dealer_hand[0][1]} ??")

        if hand_value(player_hand) == 21:
            print("Blackjack! You win!")
            return
        if hand_value(player_hand) > 21:
            print("Bust! Dealer wins.")
            return

        choice = input("Hit or stand? [h/s]: ").strip().lower()
        if choice not in {"h", "s", "hit", "stand"}:
            print("Please enter 'h' to hit or 's' to stand.")
            continue

        if choice in {"h", "hit"}:
            player_hand.append(deal_card(deck))
            continue

        break

    print("\nDealer's hand:", hand_to_string(dealer_hand), f"(value: {hand_value(dealer_hand)})")
    while hand_value(dealer_hand) < 17:
        dealer_hand.append(deal_card(deck))
        print("Dealer hits:", hand_to_string(dealer_hand), f"(value: {hand_value(dealer_hand)})")

    player_total = hand_value(player_hand)
    dealer_total = hand_value(dealer_hand)

    if dealer_total > 21:
        print("Dealer busts! You win!")
    elif dealer_total > player_total:
        print("Dealer wins.")
    elif dealer_total < player_total:
        print("You win!")
    else:
        print("Push. It's a tie.")


def main() -> None:
    print("Welcome to Blackjack! Try to get as close to 21 as possible without going over.")
    while True:
        play_round()
        again = input("\nPlay another round? [y/n]: ").strip().lower()
        if again not in {"y", "yes"}:
            print("Thanks for playing! Goodbye.")
            break


if __name__ == "__main__":
    main()
