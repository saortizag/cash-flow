from django.urls import path

from . import views

app_name = 'ledger'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),

    path('accounts/', views.AccountListView.as_view(), name='account_list'),
    path('accounts/new/', views.AccountCreateView.as_view(), name='account_create'),
    path('accounts/<int:pk>/edit/', views.AccountUpdateView.as_view(), name='account_update'),
    path('accounts/<int:pk>/delete/', views.AccountDeleteView.as_view(), name='account_delete'),

    path('categories/', views.CategoryListView.as_view(), name='category_list'),
    path('categories/new/', views.CategoryCreateView.as_view(), name='category_create'),
    path('categories/<int:pk>/edit/', views.CategoryUpdateView.as_view(), name='category_update'),
    path('categories/<int:pk>/delete/', views.CategoryDeleteView.as_view(), name='category_delete'),

    path('transactions/', views.transaction_list, name='transaction_list'),
    path('transactions/new/', views.transaction_create, name='transaction_create'),
    path('transactions/<int:pk>/edit/', views.transaction_update, name='transaction_update'),
    path('transactions/<int:pk>/delete/', views.transaction_delete, name='transaction_delete'),
    path('transactions/<int:pk>/execute/', views.transaction_execute, name='transaction_execute'),
    path('transactions/<int:pk>/unexecute/', views.transaction_unexecute, name='transaction_unexecute'),

    path('recurring/', views.RecurringListView.as_view(), name='recurring_list'),
    path('recurring/new/', views.RecurringCreateView.as_view(), name='recurring_create'),
    path('recurring/<int:pk>/edit/', views.recurring_update, name='recurring_update'),
    path('recurring/<int:pk>/delete/', views.recurring_delete, name='recurring_delete'),
    path('recurring/<int:pk>/deactivate/', views.recurring_deactivate, name='recurring_deactivate'),

    path('projection/', views.projection_view, name='projection'),
]
