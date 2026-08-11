from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from . import views

app_name = 'api'

router = DefaultRouter()
router.register('accounts', views.AccountViewSet, basename='account')
router.register('categories', views.CategoryViewSet, basename='category')
router.register('transactions', views.TransactionViewSet, basename='transaction')
router.register('recurring-transactions', views.RecurringTransactionViewSet, basename='recurringtransaction')
router.register('transfers', views.TransferViewSet, basename='transfer')
router.register('credit-card-statements', views.CreditCardStatementViewSet, basename='creditcardstatement')

urlpatterns = [
    # Infra — unversioned.
    path('token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('schema/', SpectacularAPIView.as_view(), name='schema'),
    path('docs/', SpectacularSwaggerView.as_view(url_name='api:schema'), name='swagger-ui'),

    # Resources — versioned.
    path('v1/summary/', views.SummaryView.as_view(), name='summary'),
    path('v1/projection/summary/', views.ProjectionSummaryView.as_view(), name='projection-summary'),
    path('v1/projection/detail/', views.ProjectionDetailView.as_view(), name='projection-detail'),
    path('v1/', include(router.urls)),
]
