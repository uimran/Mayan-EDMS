from __future__ import unicode_literals

from datetime import timedelta
import logging

from django.contrib.auth.models import User
from django.db import OperationalError
from django.utils.timezone import now

from mayan.celery import app

from common.models import SharedUploadedFile

from .literals import (
    UPDATE_PAGE_COUNT_RETRY_DELAY, UPLOAD_NEW_VERSION_RETRY_DELAY
)
from .models import (
    DeletedDocument, Document, DocumentPage, DocumentType, DocumentVersion
)

logger = logging.getLogger(__name__)


@app.task(ignore_result=True)
def task_check_delete_periods():
    logger.info('Executing')

    for document_type in DocumentType.objects.all():
        logger.info('Checking deletion period of document type: %s', document_type)
        if document_type.delete_time_period and document_type.delete_time_unit:
            delta = timedelta(**{document_type.delete_time_unit: document_type.delete_time_period})
            logger.info('Document type: %s, has a deletion period delta of: %s', document_type, delta)
            for document in DeletedDocument.objects.filter(document_type=document_type):
                if now() > document.deleted_date_time + delta:
                    logger.info('Document "%s" with id: %d, trashed on: %s, exceded delete period', document, document.pk, document.deleted_date_time)
                    document.delete()
        else:
            logger.info('Document type: %s, has a no retention delta', document_type)

    logger.info('Finshed')


@app.task(ignore_result=True)
def task_check_trash_periods():
    logger.info('Executing')

    for document_type in DocumentType.objects.all():
        logger.info('Checking trash period of document type: %s', document_type)
        if document_type.trash_time_period and document_type.trash_time_unit:
            delta = timedelta(**{document_type.trash_time_unit: document_type.trash_time_period})
            logger.info('Document type: %s, has a trash period delta of: %s', document_type, delta)
            for document in Document.objects.filter(document_type=document_type):
                if now() > document.date_added + delta:
                    logger.info('Document "%s" with id: %d, added on: %s, exceded trash period', document, document.pk, document.date_added)
                    document.delete()
        else:
            logger.info('Document type: %s, has a no retention delta', document_type)

    logger.info('Finshed')


@app.task(ignore_result=True)
def task_clear_image_cache():
    logger.info('Starting document cache invalidation')
    # TODO: Notification of success and of errors
    Document.objects.invalidate_cache()
    logger.info('Finished document cache invalidation')


@app.task(compression='zlib')
def task_get_document_page_image(document_page_id, *args, **kwargs):
    document_page = DocumentPage.objects.get(pk=document_page_id)
    return document_page.get_image(*args, **kwargs)


@app.task(bind=True, default_retry_delay=UPDATE_PAGE_COUNT_RETRY_DELAY, ignore_result=True)
def task_update_page_count(self, version_id):
    document_version = DocumentVersion.objects.get(pk=version_id)
    try:
        document_version.update_page_count()
    except OperationalError as exception:
        logger.warning('Operational error during attempt to update page count for document version: %s; %s. Retrying.', document_version, exception)
        raise self.retry(exc=exception)


@app.task(bind=True, default_retry_delay=UPLOAD_NEW_VERSION_RETRY_DELAY, ignore_result=True)
def task_upload_new_version(self, document_id, shared_uploaded_file_id, user_id, comment=None):
    document = Document.objects.get(pk=document_id)

    shared_file = SharedUploadedFile.objects.get(pk=shared_uploaded_file_id)

    if user_id:
        user = User.objects.get(pk=user_id)
    else:
        user = None

    with shared_file.open() as file_object:
        document_version = DocumentVersion(document=document, comment=comment or '', file=file_object)
        try:
            document_version.save(_user=user)
        except Warning as warning:
            # New document version are blocked
            logger.info('Warning during attempt to create new document version for document: %s; %s', document, warning)
            shared_file.delete()
        except OperationalError as exception:
            # Database is locked for example
            logger.warning('Operational error during attempt to create new document version for document: %s; %s. Retrying.', document, exception)
            raise self.retry(exc=exception)
        except Exception as exception:
            # This except and else block emulate a finally:
            logger.error('Unexpected error during attempt to create new document version for document: %s; %s', document, warning)
            shared_file.delete()
        else:
            shared_file.delete()
