from django.urls import path

from . import views

app_name = 'ledger'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),

    path('accounts/', views.AccountListView.as_view(), name='account_list'),
    path('accounts/new/', views.AccountCreateView.as_view(), name='account_create'),
    path('accounts/<int:pk>/edit/', views.AccountUpdateView.as_view(), name='account_update'),
    path('accounts/<int:pk>/delete/', views.AccountDeleteView.as_view(), name='account_delete'),
    path('accounts/credit-card-bootstrap/', views.credit_card_bootstrap, name='credit_card_bootstrap'),

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
    path('transactions/<int:pk>/assign-account/', views.transaction_assign_account, name='transaction_assign_account'),
    path('transactions/<int:pk>/attachment/', views.transaction_attachment, name='transaction_attachment'),
    path('transactions/<int:pk>/attachment/download/', views.transaction_attachment_download,
         name='transaction_attachment_download'),

    path('transfers/', views.transfer_list, name='transfer_list'),
    path('transfers/new/', views.transfer_create, name='transfer_create'),
    path('transfers/<int:pk>/', views.transfer_detail, name='transfer_detail'),
    path('transfers/<int:pk>/edit/', views.transfer_update, name='transfer_update'),
    path('transfers/<int:pk>/delete/', views.transfer_delete, name='transfer_delete'),
    path('transfers/<int:pk>/execute/', views.transfer_execute, name='transfer_execute'),
    path('transfers/<int:pk>/unexecute/', views.transfer_unexecute, name='transfer_unexecute'),
    path('transfers/<int:pk>/attachment/', views.transfer_attachment, name='transfer_attachment'),
    path('transfers/<int:pk>/attachment/download/', views.transfer_attachment_download,
         name='transfer_attachment_download'),

    path('recurring/', views.RecurringListView.as_view(), name='recurring_list'),
    path('recurring/new/', views.RecurringCreateView.as_view(), name='recurring_create'),
    path('recurring/<int:pk>/edit/', views.recurring_update, name='recurring_update'),
    path('recurring/<int:pk>/delete/', views.recurring_delete, name='recurring_delete'),
    path('recurring/<int:pk>/deactivate/', views.recurring_deactivate, name='recurring_deactivate'),

    path('projection/', views.projection_view, name='projection'),
]
