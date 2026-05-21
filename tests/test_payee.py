from __future__ import annotations

from inab.models import payee_from_description


def test_payee_from_twint_purchase_description() -> None:
    assert payee_from_description("Achat TWINT SBB MOBILE") == "SBB Mobile"
    assert payee_from_description("Achat TWINT SBB EASYRIDE") == "SBB EasyRide"
    assert payee_from_description("Achat TWINT DIGITEC GALAXUS") == "Digitec Galaxus"
    assert payee_from_description("Achat TWINT PARKINGPAY-TWINT") == "ParkingPay"


def test_payee_from_card_purchase_description() -> None:
    assert (
        payee_from_description(
            "Achat Coop-1959 Oron-la- 30.03.2026, 14:45, No carte Visa Debit 400000xxxxxx0002"
        )
        == "Coop"
    )
    assert (
        payee_from_description(
            "Achat Coop-12632 Parking C 30.04.2026, 14:10, No carte Visa Debit 400000xxxxxx0002"
        )
        == "Coop Parking"
    )
    assert (
        payee_from_description(
            "Achat Migros Parking Lausanne 30.04.2026, 14:10, No carte Visa Debit 400000xxxxxx0002"
        )
        == "Migros Parking"
    )
    assert (
        payee_from_description(
            "Achat Example Clinic 30.04.2026, 14:10, No carte V PAY 00000000"
        )
        == "Example Clinic"
    )


def test_payee_from_online_purchase_description() -> None:
    assert (
        payee_from_description(
            "Achat online Example Relay 30.04.2026, 01:32, No carte Visa Debit 400000xxxxxx0002"
        )
        == "Example Relay"
    )
    assert (
        payee_from_description(
            "Achat online Example Subscription 30.04.2026, 01:32, No carte Visa Debit 400000xxxxxx0002"
        )
        == "Example Subscription"
    )
    assert (
        payee_from_description(
            "Achat online BKG*BOOKING.COM HOTEL 17.04.2026, 18:25, No carte Visa Debit 400000xxxxxx0002 EUR 473.37, taux de change 0.934"
        )
        == "Booking.com"
    )
    assert (
        payee_from_description(
            "Achat online SERVICE NAVIGO 4042878 25.04.2026, 17:50, No carte Visa Debit 400000xxxxxx0002"
        )
        == "Service Navigo"
    )
    assert (
        payee_from_description(
            "Achat online SBB CFF FFS Mobile T 06.02.2026, 17:23, No carte Visa Debit 400000xxxxxx0002"
        )
        == "SBB Mobile"
    )


def test_payee_from_payment_and_credit_description() -> None:
    assert payee_from_description("Paiement Example Mobile SA") == "Example Mobile SA"
    assert payee_from_description("Paiement TWINT EXAMPLE, ALEX") == "Alex Example"
    assert payee_from_description("Crédit TWINT SAMPLE, JAMIE") == "Jamie Sample"
    assert (
        payee_from_description("Crédit EXAMPLE BENEFITS OFFICE")
        == "Example Benefits Office"
    )


def test_generic_payee_labels_are_not_cleaned_without_details() -> None:
    assert payee_from_description("Paiement groupé") == "Paiement groupé"
    assert payee_from_description("Ordre permanent") == "Ordre permanent"
