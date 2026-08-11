from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

User = get_user_model()


class JWTAuthTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='tester', password='pw12345!')

    def test_obtain_token_with_valid_credentials(self):
        response = self.client.post(reverse('api:token_obtain_pair'),
                                     {'username': 'tester', 'password': 'pw12345!'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('access', response.data)
        self.assertIn('refresh', response.data)

    def test_obtain_token_with_invalid_credentials_fails(self):
        response = self.client.post(reverse('api:token_obtain_pair'),
                                     {'username': 'tester', 'password': 'wrong'})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_unauthenticated_request_is_rejected(self):
        response = self.client.get(reverse('api:account-list'))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_obtained_access_token_authenticates_a_request(self):
        token_response = self.client.post(reverse('api:token_obtain_pair'),
                                           {'username': 'tester', 'password': 'pw12345!'})
        access = token_response.data['access']
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')
        response = self.client.get(reverse('api:account-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_refresh_token_issues_a_new_access_token(self):
        token_response = self.client.post(reverse('api:token_obtain_pair'),
                                           {'username': 'tester', 'password': 'pw12345!'})
        refresh = token_response.data['refresh']
        response = self.client.post(reverse('api:token_refresh'), {'refresh': refresh})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('access', response.data)
