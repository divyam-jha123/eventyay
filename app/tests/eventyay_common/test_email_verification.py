import smtplib

import pytest
from django.core.cache import cache
from django.core.mail.backends.locmem import EmailBackend
from django.template.loader import render_to_string
from django.urls import reverse

from eventyay.base.models import User
from eventyay.helpers.jinja import contact_mailto


SENT_URL = '/accounts/confirm-email/'
PASSWORD = 'Sup3r-Secret-pw!'
RESEND_BUTTON = 'Resend verification email'
COOLDOWN_MESSAGE = 'A verification email was sent recently.'
SEND_FAILED_MESSAGE = 'We could not send the verification email.'
OUT_OF_DATE_MESSAGE = 'This page was out of date.'
NO_PENDING_MESSAGE = 'There is no email verification pending in this browser.'


@pytest.fixture(autouse=True)
def locmem_cache(settings):
    # allauth's resend cooldown is stored in the default cache.
    settings.CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'email-verification-testing-cache',
        }
    }
    cache.clear()
    yield
    cache.clear()


def sign_up(client, email):
    response = client.post(
        reverse('account_signup'),
        {'email': email, 'password1': PASSWORD, 'password2': PASSWORD},
    )
    assert response.status_code == 302
    assert response['Location'] == SENT_URL


@pytest.mark.django_db
def test_page_shows_pending_address_and_resend_button(client):
    sign_up(client, 'new@example.com')

    content = client.get(SENT_URL).content.decode()

    assert 'new@example.com' in content
    assert RESEND_BUTTON in content


@pytest.mark.django_db
def test_resend_sends_confirmation_email(client, mailoutbox):
    sign_up(client, 'new@example.com')
    # Skip the cooldown started by the signup email.
    cache.clear()

    response = client.post(SENT_URL, {'email': 'new@example.com'}, follow=True)

    assert response.redirect_chain == [(SENT_URL, 302)]
    assert len(mailoutbox) == 2
    assert mailoutbox[-1].to == ['new@example.com']
    assert 'Confirmation email sent to new@example.com.' in response.content.decode()


@pytest.mark.django_db
def test_resend_within_cooldown_shows_error_and_sends_nothing(client, mailoutbox):
    sign_up(client, 'new@example.com')

    response = client.post(SENT_URL, {'email': 'new@example.com'}, follow=True)

    assert response.redirect_chain == [(SENT_URL, 302)]
    assert len(mailoutbox) == 1
    assert COOLDOWN_MESSAGE in response.content.decode()


@pytest.mark.django_db
def test_existing_address_looks_like_a_new_signup(client, mailoutbox):
    User.objects.create_user(email='taken@example.com', password=PASSWORD)
    sign_up(client, 'taken@example.com')
    content = client.get(SENT_URL).content.decode()
    assert 'taken@example.com' in content
    assert RESEND_BUTTON in content
    cache.clear()

    response = client.post(SENT_URL, {'email': 'taken@example.com'}, follow=True)

    assert 'Confirmation email sent to taken@example.com.' in response.content.decode()
    assert len(mailoutbox) == 2
    # allauth emails the owner instead of sending a confirmation link.
    assert 'Account Already Exists' in mailoutbox[-1].subject
    assert mailoutbox[-1].to == ['taken@example.com']


@pytest.mark.django_db
def test_expired_pending_login_has_no_resend(client, mailoutbox, settings):
    sign_up(client, 'new@example.com')
    # Display (and consume) the signup's "sent" message before the pending login expires.
    client.get(SENT_URL)
    cache.clear()
    settings.ACCOUNT_LOGIN_TIMEOUT = -1

    response = client.post(SENT_URL, {'email': 'new@example.com'}, follow=True)
    content = response.content.decode()

    assert response.redirect_chain == [(SENT_URL, 302)]
    assert len(mailoutbox) == 1
    assert RESEND_BUTTON not in content
    assert 'new@example.com' not in content
    assert COOLDOWN_MESSAGE not in content


@pytest.mark.django_db
def test_page_without_pending_signup_has_no_resend(client, mailoutbox):
    response = client.post(SENT_URL, {'email': 'new@example.com'}, follow=True)
    content = response.content.decode()

    assert len(mailoutbox) == 0
    assert RESEND_BUTTON not in content
    assert NO_PENDING_MESSAGE in content
    assert "We've sent" not in content


@pytest.mark.django_db
def test_resend_mail_failure_shows_retryable_error(client, mailoutbox, monkeypatch):
    sign_up(client, 'new@example.com')
    cache.clear()

    def fail(self, messages):
        raise smtplib.SMTPServerDisconnected('Connection unexpectedly closed')

    with monkeypatch.context() as patched:
        patched.setattr(EmailBackend, 'send_messages', fail)
        response = client.post(SENT_URL, {'email': 'new@example.com'}, follow=True)

    assert response.status_code == 200
    assert SEND_FAILED_MESSAGE in response.content.decode()
    assert len(mailoutbox) == 1

    # The failed attempt must not start the cooldown.
    response = client.post(SENT_URL, {'email': 'new@example.com'}, follow=True)

    assert 'Confirmation email sent to new@example.com.' in response.content.decode()
    assert len(mailoutbox) == 2


@pytest.mark.django_db
def test_resend_from_stale_tab_does_not_send_to_newer_signup(client, mailoutbox):
    sign_up(client, 'first@example.com')
    # A second signup in another tab replaces the pending login in the shared session.
    sign_up(client, 'second@example.com')
    cache.clear()

    response = client.post(SENT_URL, {'email': 'first@example.com'}, follow=True)
    content = response.content.decode()

    assert len(mailoutbox) == 2
    assert OUT_OF_DATE_MESSAGE in content
    assert 'second@example.com' in content


@pytest.mark.django_db
def test_signup_email_uses_shared_html_layout_and_keeps_plain_text(client, mailoutbox):
    sign_up(client, 'new@example.com')

    message = mailoutbox[0]
    html, mimetype = message.alternatives[0]
    activate_url = message.body.split('go to ')[1].split()[0]

    assert mimetype == 'text/html'
    assert len(message.alternatives) == 1
    assert activate_url.startswith('http')
    assert html.count(f'href="{activate_url}"') == 2  # button and fallback link
    assert 'Verify email address' in html
    assert 'Thanks for signing up for' in html
    assert 'new@example.com' in html
    # Rendered through pretixbase/email/base.html, with its CSS inlined.
    assert '<table class="layout"' in html
    assert 'style="width: 100%;max-width: 600px;' in html
    assert 'powered by' in html
    # The plain-text alternative is still allauth's default.
    assert 'To confirm this is correct, go to' in message.body


def test_add_email_confirmation_uses_non_signup_wording():
    html = render_to_string(
        'eventyay_common/email/email_confirmation.html',
        {'email': 'extra@example.com', 'activate_url': 'https://example.com/confirm/KEY/', 'site': 'eventyay'},
    )

    assert 'Please confirm that extra@example.com is your email address' in html
    assert 'Thanks for signing up' not in html
    assert 'href="https://example.com/confirm/KEY/"' in html


def test_confirmation_email_with_code_shows_code_instead_of_link():
    html = render_to_string(
        'eventyay_common/email/email_confirmation_signup.html',
        {'email': 'new@example.com', 'code': 'XK42PQ', 'site': 'eventyay'},
    )

    assert 'XK42PQ' in html
    assert 'Verify email address' not in html
    assert 'Thanks for signing up for' in html


@pytest.mark.parametrize(
    ('from_email', 'expected'),
    [
        ('info@example.com', 'mailto:info@example.com'),
        ('Eventyay Team <info+help@example.com>', 'mailto:info%2Bhelp@example.com'),
        ('', ''),
    ],
)
def test_contact_mailto(settings, from_email, expected):
    settings.DEFAULT_FROM_EMAIL = from_email

    assert contact_mailto() == expected
