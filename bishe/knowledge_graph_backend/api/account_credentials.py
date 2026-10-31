"""Resolve login identifiers without changing users or bypassing Django auth."""
from collections.abc import Mapping
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import validate_email


def login_username(identifier):
    # Preserve existing literal usernames, including legacy unusual usernames.
    # An exact username always wins; a wrong password never falls into another
    # user's email identity.
    if User.objects.filter(username=identifier).exists():
        return identifier
    cleaned = User.normalize_username(identifier.strip())
    if User.objects.filter(username=cleaned).exists():
        return cleaned
    if '@' not in cleaned:
        return cleaned
    matches = list(User.objects.filter(email__iexact=cleaned).values_list('username', flat=True)[:2])
    # Old databases may contain repeated email addresses. Never choose one.
    return matches[0] if len(matches) == 1 else cleaned


def registration_fields(data):
    if not isinstance(data, Mapping):
        raise ValueError('请提供用户名、邮箱和密码')
    username, password, email = (data.get(k) for k in ('username', 'password', 'email'))
    if not all(isinstance(x, str) and x for x in (username, password, email)):
        raise ValueError('请提供用户名、邮箱和密码')
    username = User.normalize_username(username.strip())
    email = email.strip()
    if len(email) > 254:
        raise ValueError('邮箱过长')
    if not 3 <= len(username) <= 20:
        raise ValueError('用户名长度需为3–20个字符')
    if not 6 <= len(password) <= 1024 or not password.strip():
        raise ValueError('密码需为6–1024个字符，不能全为空白')
    try:
        User.username_validator(username)
        validate_email(email)
    except ValidationError:
        raise ValueError('请使用有效的用户名和邮箱') from None
    return username, password, email


def registration_conflict(username, email):
    if User.objects.filter(username=username).exists():
        return '该用户名已存在，请登录或更换用户名'
    if User.objects.filter(email__iexact=email).exists():
        return '该邮箱已注册，请直接登录'
    # Prevent new accounts from making a different account's email login shadowed
    # by an email-shaped username (or the reverse).
    if ('@' in username and User.objects.filter(email__iexact=username).exists()
            or User.objects.filter(username__iexact=email).exists()):
        return '用户名或邮箱与已有账号冲突，请更换用户名或使用原账号登录'
    return None
