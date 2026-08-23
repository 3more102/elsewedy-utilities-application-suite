import asyncio
from pathlib import Path

from app.production import (
    ProductionPrivateUploadBoundary,
    ProductionSecurityHeaders,
    STRICT_CONTENT_SECURITY_POLICY,
    STRICT_TRANSPORT_SECURITY,
)


ROOT = Path(__file__).resolve().parents[1]


def _run_path(path: str):
    reached = []

    async def legacy_app(scope, receive, send):
        reached.append(scope.get('path'))
        body = b'legacy-public-upload-content'
        await send({
            'type': 'http.response.start',
            'status': 200,
            'headers': [(b'content-type', b'application/octet-stream')],
        })
        await send({'type': 'http.response.body', 'body': body})

    sent = []

    async def receive():
        return {'type': 'http.disconnect'}

    async def send(message):
        sent.append(message)

    application = ProductionSecurityHeaders(
        ProductionPrivateUploadBoundary(legacy_app)
    )
    asyncio.run(
        application(
            {
                'type': 'http',
                'method': 'GET',
                'path': path,
                'scheme': 'https',
                'headers': [],
            },
            receive,
            send,
        )
    )
    return reached, sent


def test_production_blocks_legacy_public_upload_mount_before_routing():
    for path in ('/uploads', '/uploads/randomized-secret.pdf'):
        reached, sent = _run_path(path)
        assert reached == []
        assert sent[0]['status'] == 404
        headers = {name.lower(): value for name, value in sent[0]['headers']}
        assert headers[b'cache-control'] == b'no-store, private, max-age=0'
        assert headers[b'content-security-policy'] == STRICT_CONTENT_SECURITY_POLICY.encode('ascii')
        assert headers[b'strict-transport-security'] == STRICT_TRANSPORT_SECURITY.encode('ascii')
        assert sent[1]['body'] == b'Not Found'

    application_source = (ROOT / 'app' / 'application.py').read_text(encoding='utf-8')
    production_source = (ROOT / 'app' / 'production.py').read_text(encoding='utf-8')
    assert "app.mount('/uploads',StaticFiles(directory=UPLOAD_DIR),name='uploads')" in application_source
    assert 'ProductionPrivateUploadBoundary(_application)' in production_source


def test_authenticated_document_download_route_remains_reachable_through_boundary():
    reached, sent = _run_path('/api/documents/17/download')
    assert reached == ['/api/documents/17/download']
    assert sent[0]['status'] == 200
    headers = {name.lower(): value for name, value in sent[0]['headers']}
    assert headers[b'cache-control'] == b'no-store, private, max-age=0'
    assert headers[b'content-security-policy'] == STRICT_CONTENT_SECURITY_POLICY.encode('ascii')
    assert sent[1]['body'] == b'legacy-public-upload-content'

    application_source = (ROOT / 'app' / 'application.py').read_text(encoding='utf-8')
    assert "@app.get('/api/documents/{doc_id}/download')" in application_source
    assert 'user=Depends(current_user)' in application_source
    assert "return FileResponse(p,filename=d['file_name']" in application_source
