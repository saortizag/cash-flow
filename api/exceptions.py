"""
Every check-then-act guard in ledger/services.py raises Django's own
django.core.exceptions.ValidationError, not DRF's. DRF's default exception
handler only special-cases Http404/PermissionDenied/APIException subclasses,
so left alone a Django ValidationError propagating out of a view would
surface as an unhandled 500. This handler translates it (and
django.db.models.ProtectedError, raised when deleting an Account/Category
still referenced by a PROTECT foreign key) into a clean 400 response, so
viewset code can call straight into services.py without a try/except at
every call site.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import ProtectedError
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler


def custom_exception_handler(exc, context):
    if isinstance(exc, DjangoValidationError):
        detail = exc.message_dict if hasattr(exc, 'message_dict') else exc.messages
        return Response({'detail': detail}, status=status.HTTP_400_BAD_REQUEST)

    if isinstance(exc, ProtectedError):
        return Response(
            {'detail': 'Cannot delete: this row is still referenced by other records.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    return drf_exception_handler(exc, context)
