"""Real Django password/token authentication on the isolated test database."""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import DatabaseError
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase


class AccountCredentialTests(APITestCase):
    def register(self, username='auth-synthetic', email='auth@example.test', password='Synthetic-only-123'):
        return self.client.post('/api/auth/register/', {'username': username, 'email': email, 'password': password}, format='json')

    def login(self, identifier='auth-synthetic', password='Synthetic-only-123'):
        return self.client.post('/api/auth/login/', {'username': identifier, 'password': password}, format='json')

    def test_register_persists_hashed_password_and_login_by_username(self):
        self.assertEqual(self.register().status_code, 201)
        u = User.objects.get(username='auth-synthetic')
        self.assertNotEqual(u.password, 'Synthetic-only-123')
        self.assertTrue(u.check_password('Synthetic-only-123'))
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Token.objects.get(key=response.data['token']).user_id, u.pk)
        self.assertNotIn('password', response.data['user'])

    def test_unique_email_login_uses_same_user_and_token(self):
        self.register()
        first, second = self.login(), self.login('auth@example.test')
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.data['token'], second.data['token'])
        self.assertEqual(first.data['user'], second.data['user'])

    def test_email_case_and_surrounding_spaces(self):
        self.register(email='Case@Example.Test')
        self.assertEqual(self.login('  CASE@example.test  ').status_code, 200)

    def test_password_is_not_trimmed(self):
        self.register(password='  Synthetic-only-123  ')
        self.assertEqual(self.login('auth@example.test', '  Synthetic-only-123  ').status_code, 200)
        self.assertEqual(self.login('auth@example.test', 'Synthetic-only-123').status_code, 401)

    def test_wrong_password_and_unknown_email_same_failure(self):
        self.register()
        first, second = self.login('auth@example.test', 'wrong'), self.login('missing@example.test', 'wrong')
        self.assertEqual(first.status_code, 401)
        self.assertEqual(first.data, second.data)
        self.assertEqual(Token.objects.count(), 0)

    def test_inactive_user_cannot_login(self):
        self.register(); User.objects.filter(username='auth-synthetic').update(is_active=False)
        self.assertEqual(self.login('auth@example.test').status_code, 401)
        self.assertEqual(Token.objects.count(), 0)

    def test_repeated_email_registration_case_insensitive_refused(self):
        self.register(email='same@example.test')
        response = self.register(username='other-synthetic', email='SAME@example.test')
        self.assertEqual(response.status_code, 400)
        self.assertIn('邮箱', response.data['error'])
        self.assertEqual(User.objects.count(), 1)

    def test_duplicate_username_refused_without_mutating_old_password(self):
        self.register()
        response = self.register(email='different@example.test', password='Changed-password')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(self.login().status_code, 200)

    def test_old_duplicate_email_refused_but_usernames_still_work(self):
        self.register(email='shared@example.test')
        User.objects.create_user('old-synthetic', 'SHARED@example.test', 'Legacy-only-123')
        self.assertEqual(self.login('shared@example.test').status_code, 401)
        self.assertEqual(self.login('auth-synthetic').status_code, 200)
        self.assertEqual(self.login('old-synthetic', 'Legacy-only-123').status_code, 200)

    def test_existing_email_shaped_username_priority_no_wrong_password_fallback(self):
        self.register(username='owner-synthetic', email='shared@example.test')
        User.objects.create_user('shared@example.test', 'other@example.test', 'Legacy-only-123')
        self.assertEqual(self.login('shared@example.test').status_code, 401)
        response = self.login('shared@example.test', 'Legacy-only-123')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['user']['username'], 'shared@example.test')

    def test_new_username_cannot_shadow_existing_email(self):
        self.register(email='auth@example.test')
        self.assertEqual(self.register(username='auth@example.test', email='second@example.test').status_code, 400)
        self.assertEqual(User.objects.count(), 1)

    def test_new_email_cannot_collide_with_existing_username(self):
        User.objects.create_user('legacy@example.test', 'other@example.test', 'Legacy-only-123')
        self.assertEqual(self.register(email='legacy@example.test').status_code, 400)
        self.assertEqual(User.objects.count(), 1)

    def test_normalize_registration_and_keep_legacy_literal_username(self):
        self.assertEqual(self.register(username='  auth-synthetic  ', email='  auth@example.test  ').status_code, 201)
        self.assertEqual(self.login(' auth-synthetic ').status_code, 200)
        User.objects.create_user(' legacy-space ', 'legacy@example.test', 'Legacy-only-123')
        self.assertEqual(self.login(' legacy-space ', 'Legacy-only-123').status_code, 200)

    def test_bad_fields_fail_without_partial_user(self):
        for body in [{}, [], {'username': ['x'], 'password': 'Synthetic-only-123', 'email': 'a@example.test'},
                     {'username': 'valid-name', 'password': 'x', 'email': 'a@example.test'},
                     {'username': 'valid-name', 'password': 'Synthetic-only-123', 'email': 'invalid'},
                     {'username': 'bad name', 'password': 'Synthetic-only-123', 'email': 'a@example.test'}]:
            with self.subTest(body_type=type(body).__name__):
                self.assertEqual(self.client.post('/api/auth/register/', body, format='json').status_code, 400)
        self.assertEqual(User.objects.count(), 0)

    def test_malformed_login_fields_return_400(self):
        for body in [[], {}, {'username': 123, 'password': 'test-only'}, {'username': 'abc', 'password': ['x']}]:
            self.assertEqual(self.client.post('/api/auth/login/', body, format='json').status_code, 400)

    def test_stale_token_does_not_block_public_auth(self):
        self.client.credentials(HTTP_AUTHORIZATION='Token expired-synthetic-token')
        self.assertEqual(self.register().status_code, 201)
        self.assertEqual(self.login('auth@example.test').status_code, 200)
        # A stale header still cannot access private chat routes.
        self.assertEqual(self.client.get('/api/conversations/').status_code, 401)

    def test_registration_database_error_is_not_fake_duplicate(self):
        with patch('api.views.User.objects.create_user', side_effect=DatabaseError('private diagnostics')):
            response = self.register()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('private', str(response.data))

    def test_token_reads_only_own_history(self):
        from .models import Conversation
        self.register(); other = User.objects.create_user('other-synthetic', 'other@example.test', 'Other-only-123')
        Conversation.objects.create(user=other, title='other private synthetic')
        response = self.login('auth@example.test')
        self.client.credentials(HTTP_AUTHORIZATION='Token '+response.data['token'])
        self.assertEqual(self.client.get('/api/conversations/').data, [])
        own = self.client.post('/api/conversations/', {'title': 'own synthetic'}, format='json')
        self.assertEqual(own.status_code, 201)
        self.assertEqual(len(self.client.get('/api/conversations/').data), 1)
