import pytest

from app import clinics
from app.services import safety


@pytest.fixture
def clinic():
    return clinics.load_clinic("demo_clinic")


def test_normal_reply_passes(clinic):
    ok, reasons = safety.check_reply(
        "Hi! Consultation is ₹300. Book here: https://cal.com/REPLACE/consultation", clinic)
    assert ok, reasons


def test_listed_amount_with_commas_or_text(clinic):
    ok, _ = safety.check_reply("Cleaning starts at Rs. 800 onwards.", clinic)
    assert ok


@pytest.mark.parametrize("reply", [
    "Root canal costs ₹9,999.",
    "It is only 5000 rupees.",
    "Price: INR 1200.",
])
def test_unlisted_amounts_rejected(clinic, reply):
    ok, reasons = safety.check_reply(reply, clinic)
    assert not ok
    assert any("amount" in r for r in reasons)


@pytest.mark.parametrize("reply", [
    "We guarantee great results.",
    "This will cure your cavity.",
    "100% safe treatment.",
    "The procedure is painless.",
    "Take ibuprofen after the visit.",
    "You may need antibiotics.",
])
def test_forbidden_terms_rejected(clinic, reply):
    ok, _ = safety.check_reply(reply, clinic)
    assert not ok


def test_word_boundaries_avoid_false_positives(clinic):
    ok, reasons = safety.check_reply("Your data is secure with us. Book a visit soon.", clinic)
    assert ok, reasons


def test_too_long_rejected(clinic):
    ok, reasons = safety.check_reply("word " * 81, clinic)
    assert not ok
    assert any("longer" in r for r in reasons)


def test_empty_rejected(clinic):
    assert safety.check_reply("   ", clinic)[0] is False


def test_safe_reply_passes_own_check(clinic):
    ok, reasons = safety.check_reply(safety.safe_reply(clinic), clinic)
    assert ok, reasons


@pytest.mark.parametrize("reply", [
    "$9999", "9999 INR", "₹300.99", "₹700016", "€9999", "9999£",
    "USD9999.95", "9999.95EUR", "GBP 9,999.95", "Rs9999", "9999/-",
])
def test_currency_markers_and_full_decimal_amounts_rejected(clinic, reply):
    ok, reasons = safety.check_reply(reply, clinic)
    assert not ok
    assert any("amount" in reason for reason in reasons)


@pytest.mark.parametrize("reply", ["₹300", "Rs 800", "300 rupees", "INR300.00", "800/-"])
def test_approved_currency_amounts_allowed(clinic, reply):
    assert safety.check_reply(reply, clinic)[0]


def test_only_prices_values_approve_amounts(clinic):
    clinic["phone"] = "9999"
    clinic["location"] = "9999 Street"
    clinic["prices"] = {"9999": "Doctor will confirm"}
    assert not safety.check_reply("₹9999", clinic)[0]
    assert not safety.check_reply("₹300", clinic)[0]


def test_approved_decimal_is_not_truncated(clinic):
    clinic["prices"] = {"consultation": "₹300.99", "cleaning": "₹800.50"}
    assert safety.check_reply("USD 300.990 and 800.50 EUR", clinic)[0]
    assert not safety.check_reply("₹300", clinic)[0]
    assert not safety.check_reply("₹300.999", clinic)[0]


@pytest.mark.parametrize("url", [
    "https://evil.example/book", "http://cal.com/REPLACE/consultation",
    "https://cal.com/REPLACE/consultation?redirect=evil",
    "https://cal.com/REPLACE/consultation/extra", "www.evil.example",
    "ftp://evil.example/file", "https://cal.com.evil.example/REPLACE/consultation",
])
def test_unapproved_links_rejected(clinic, url):
    ok, reasons = safety.check_reply(f"Book here: {url}", clinic)
    assert not ok
    assert any("URL" in reason for reason in reasons)


def test_only_exact_clinic_links_allowed(clinic):
    for key in ("booking_link", "maps_link"):
        url = clinic[key]
        assert safety.check_reply(f"Visit {url}", clinic)[0]
        assert safety.check_reply(f"[Visit]({url}).", clinic)[0]
    assert not safety.check_reply(
        f"Visit {clinic['booking_link']} and https://evil.example", clinic)[0]


def test_no_configured_links_allows_no_urls():
    assert not safety.check_reply("Visit https://evil.example", {})[0]


@pytest.mark.parametrize("reply", [
    "₹.99", "₹30", "$9999", "9999 INR", "₹300.99", "₹700016", "₹5k",
    ".99 rupees", "₹1,00,000", "1 lakh INR", "INR 1.5 lac", "£.99",
])
def test_unapproved_amount_forms_are_rejected(clinic, reply):
    clinic["prices"]["consultation"] = "₹300 for 30 minutes"
    ok, reasons = safety.check_reply(reply, clinic)
    assert not ok
    assert any("amount" in reason for reason in reasons)


@pytest.mark.parametrize("reply", ["₹300", "Rs 800", "300 rupees"])
def test_currency_approved_despite_descriptive_numbers(clinic, reply):
    clinic["prices"]["consultation"] = "₹300 for a 30 minute appointment"
    assert safety.check_reply(reply, clinic)[0]


@pytest.mark.parametrize("reply", [
    "₹1,00,000", "100,000 INR", "Rs 100000", "₹100k", "1 lakh rupees", "1 lac INR",
    "₹1 LAKH", "₹.5 lakh", "50k USD", "GBP 0.5 lac", "₹1.25 lakhs", "1.25 lacs INR",
])
def test_scaled_and_grouped_values_use_exact_amounts(clinic, reply):
    clinic["prices"] = {"implant": "₹1,00,000", "other": "INR 50000", "braces": "₹125000"}
    assert safety.check_reply(reply, clinic)[0]


@pytest.mark.parametrize("marker", ["₹", "$", "€", "£", "INR", "USD", "EUR", "GBP", "Rs", "rupees", "/-"])
@pytest.mark.parametrize("prefix", [True, False])
def test_currency_markers_on_either_side_of_leading_decimal(clinic, marker, prefix):
    reply = f"{marker}.99" if prefix else f".99{marker}"
    assert not safety.check_reply(reply, clinic)[0]
    clinic["prices"] = {"test": "₹0.99"}
    assert safety.check_reply(reply, clinic)[0]


@pytest.mark.parametrize("price", ["800.50", 800.50, "800", "30 minute appointment"])
def test_bare_price_number_warns_and_is_not_approved(clinic, caplog, price):
    clinic["prices"] = {"consultation": price}
    assert not safety.check_reply("₹800.50", clinic)[0]
    assert not safety.check_reply("₹800", clinic)[0]
    assert not safety.check_reply("₹30", clinic)[0]
    assert "without a currency marker" in caplog.text


def test_descriptive_number_does_not_approve_price(clinic, caplog):
    clinic["prices"] = {"consultation": "₹300 for a 30 minute appointment"}
    assert safety.check_reply("₹300", clinic)[0]
    assert not safety.check_reply("₹30", clinic)[0]
    assert "without a currency marker" in caplog.text


def test_multipliers_do_not_round_decimal_values(clinic):
    from decimal import localcontext

    clinic["prices"] = {"test": "₹123456789012345678901234567890.123k"}
    with localcontext() as context:
        context.prec = 6
        assert safety.check_reply("INR 123456789012345678901234567890123", clinic)[0]
        assert not safety.check_reply("INR 123456789012345678901234567890124", clinic)[0]


@pytest.mark.parametrize("reply", ["300 INR 9999", "9999 INR 300"])
def test_shared_currency_marker_cannot_hide_amount(clinic, reply):
    assert not safety.check_reply(reply, clinic)[0]


@pytest.mark.parametrize("url", [
    "//evil.example/book", "evil.example", "www.evil.com", "evil-example.co.uk/book",
    "https://cal.com/x?utm=1", "https://cal.com/x?", "https://cal.com/x#",
    "https://cal.com/x#section", "custom://evil.example/book",
    "[Book](https://evil.example/book)", "(https://evil.example/book)",
])
def test_url_like_tokens_are_rejected(clinic, url):
    clinic["booking_link"] = "https://cal.com/x"
    ok, reasons = safety.check_reply(f"Book here: {url}", clinic)
    assert not ok
    assert any("URL" in reason for reason in reasons)


@pytest.mark.parametrize("wrapped", [
    "https://cal.com/x", "[Book](https://cal.com/x)", "(https://cal.com/x)",
    '"https://cal.com/x"', "'https://cal.com/x'", "https://cal.com/x.,;:!)]",
])
def test_only_sentence_punctuation_is_removed_from_url(clinic, wrapped):
    clinic["booking_link"] = "https://cal.com/x"
    assert safety.check_reply(f"Book here: {wrapped}", clinic)[0]


def test_optional_website_and_email_are_allowlisted(clinic):
    clinic.update(website="https://clinic.example", email="bookings@clinic.example")
    assert safety.check_reply("Visit https://clinic.example or email bookings@clinic.example.", clinic)[0]
    assert not safety.check_reply("Visit clinic.example", clinic)[0]
    assert not safety.check_reply("Email attacker@clinic.example", clinic)[0]


def test_explicitly_approved_query_is_preserved(clinic):
    clinic["booking_link"] = "https://cal.com/x?service=consultation"
    assert safety.check_reply("Book: https://cal.com/x?service=consultation", clinic)[0]
    assert not safety.check_reply("Book: https://cal.com/x?service=consultation#", clinic)[0]
