#!/usr/bin/env python

"""
    API routes for Lenny,
    including the root endpoint and upload endpoint.

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

import json
import httpx
from functools import wraps
from typing import Optional, Generator, List
from urllib.parse import urlencode
from fastapi import (
    APIRouter,
    Request,
    UploadFile,
    File,
    Form,
    HTTPException,
    status,
    Body,
    Cookie
)
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
    JSONResponse,
)
from lenny.core import auth
from lenny.core.api import LennyAPI
from pyopds2_lenny import LennyDataProvider
from lenny.core.exceptions import (
    INVALID_ITEM,
    InvalidFileError,
    ItemExistsError,
    ItemNotFoundError,
    LoanNotRequiredError,
    DatabaseInsertError,
    FileTooLargeError,
    S3UploadError,
    UploaderNotAllowedError,
)
from lenny.core.readium import ReadiumAPI
from lenny.core.models import Item
from urllib.parse import quote

COOKIES_MAX_AGE = 604800  # 1 week


def _build_oauth_fragment(session_cookie: str, state: str = None) -> dict:
    """Build OAuth token fragment for redirect URL or opds:// callback."""
    auth_doc_id = quote(LennyAPI.make_url("/v1/api/oauth/implicit"), safe='')
    fragment = {
        "id": auth_doc_id,
        "access_token": session_cookie,
        "token_type": "bearer",
        "expires_in": auth.COOKIE_TTL
    }
    if state:
        fragment["state"] = state
    return fragment


async def _parse_request_body(request: Request) -> dict:
    """Parse request body from JSON or form data, with fallback to empty dict."""
    try:
        return await request.json()
    except:
        try:
            form = await request.form()
            return dict(form)
        except:
            return {}


def _set_session_cookie(response, session_cookie: str):
    """Set session cookie with standard security settings."""
    response.set_cookie(
        key="session",
        value=session_cookie,
        max_age=auth.COOKIE_TTL,
        httponly=True,
        secure=True,
        samesite="Lax",
        path="/"
    )
    return response


router = APIRouter()

def requires_item_auth(do_function=None):
    """
    Decorator checks item existence and gets email of
    authenticated patron and passes them to the wrapped function
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(
                request: Request, book_id: str, format: str = "epub",
                session: Optional[str] = Cookie(None),
                # Email and Item will get magically injected by this decorator and
                # passed in to the wrapped function
                email=None, item=None, *args, **kwargs):
            # Check for Bearer token
            if not session:
                auth_header = request.headers.get("Authorization")
                if auth_header and auth_header.startswith("Bearer "):
                    session = auth_header.split(" ")[1]

            if item := Item.exists(book_id):
                result = LennyAPI.auth_check(item, session=session, request=request)
                email = result.get('email', '')
                if 'error' in result:
                    # Return 401 with partial Auth Doc as per OPDS 2.0
                    return JSONResponse(
                        status_code=401, 
                        content=LennyDataProvider.get_authentication_document(),
                        media_type="application/opds-authentication+json"
                    )
 
                # NB: Email and Item will be passed into any function decorated by requires_item_auth
                return await func(
                    request=request, book_id=book_id, format=format, session=session,
                    email=email, item=item, *args, **kwargs
                )            
            return JSONResponse(status_code=401, content={"detail": "Invalid item"})    
        return wrapper
    return decorator

@router.get('/', status_code=status.HTTP_200_OK)
async def home(request: Request):
    kwargs = {"request": request}
    return request.app.templates.TemplateResponse("index.html", kwargs)

@router.get("/items")
async def get_items(fields: Optional[str]=None, offset: Optional[int]=None, limit: Optional[int]=None):
    fields = fields.split(",") if fields else None
    return LennyAPI.get_enriched_items(
        fields=fields, offset=offset, limit=limit
    )

@router.get("/opds")
async def get_opds_catalog(request: Request, offset: Optional[int]=None, limit: Optional[int]=None):
    return Response(
        content=json.dumps(
            LennyAPI.opds_feed(offset=offset, limit=limit)
        ),
        media_type="application/opds+json"
    )

@router.get("/opds/{book_id}")
async def get_opds_item(request: Request, book_id:int):
    return Response(
        content=json.dumps(
            LennyAPI.opds_feed(olid=book_id)
        ),
        media_type="application/opds-publication+json"
    )

# Redirect to the Thorium Web Reader
@router.get("/items/{book_id}/read")
@requires_item_auth()
async def redirect_reader(request: Request, book_id: str, format: str = "epub", session: Optional[str] = Cookie(None), item=None, email: str=''):
    manifest_uri = LennyAPI.make_manifest_url(book_id)
    # URL encode the manifest URI for use as a path parameter
    encoded_manifest_uri = quote(manifest_uri, safe='')
    reader_url = LennyAPI.make_url(f"/read/manifest/{encoded_manifest_uri}")
    return RedirectResponse(url=reader_url, status_code=307)

@router.get("/items/{book_id}/readium/manifest.json")
@requires_item_auth()
async def get_manifest(request: Request, book_id: str, format: str=".epub", session: Optional[str] = Cookie(None), item=None, email: str=''):
    return ReadiumAPI.get_manifest(book_id, format)

# Proxy all other readium requests
@router.get("/items/{book_id}/readium/{readium_path:path}")
@requires_item_auth()
async def proxy_readium(request: Request, book_id: str, readium_path: str, format: str=".epub", session: Optional[str] = Cookie(None), item=None, email: str=''):
    readium_url = ReadiumAPI.make_url(book_id, format, readium_path)
    with httpx.Client() as client:
        r = client.get(readium_url, params=dict(request.query_params))
        if readium_url.endswith('.json'):
            return r.json()
        content_type = r.headers.get("Content-Type", "application/octet-stream")
        return Response(content=r.content, media_type=content_type)


@router.post('/items/{book_id}/borrow')
@requires_item_auth()
async def borrow_item(request: Request, book_id: int, format: str=".epub",  session: Optional[str] = Cookie(None), item=None, email: str=''):
    """
    Handles the borrowing of a book for a patron.
    Redirects to the reader after successful borrow, similar to Internet Archive.
    """
    try:
        loan = item.borrow(email)
        # Redirect to the reader after successful borrow
        manifest_uri = LennyAPI.make_manifest_url(book_id)
        encoded_manifest_uri = quote(manifest_uri, safe='')
        reader_url = LennyAPI.make_url(f"/read/manifest/{encoded_manifest_uri}")
        return RedirectResponse(url=reader_url, status_code=303)
    except LoanNotRequiredError as e:
        # For open-access items, redirect to read
        manifest_uri = LennyAPI.make_manifest_url(book_id)
        encoded_manifest_uri = quote(manifest_uri, safe='')
        reader_url = LennyAPI.make_url(f"/read/manifest/{encoded_manifest_uri}")
        return RedirectResponse(url=reader_url, status_code=303)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post('/items/{book_id}/return', status_code=status.HTTP_200_OK)
@requires_item_auth()
async def return_item(request: Request, book_id: int, format: str=".epub", session: Optional[str] = Cookie(None), item=None, email: str=''):
    """
    Handles the return process for a single borrowed book. Requires patron's email.
    Calls return_items to mark the loan as returned.
    """
    try:
        loan = item.unborrow(email)
        return JSONResponse(content={
            "success": True,
            "email": email,
            "loan_id": loan.id,
            "item_id": book_id
        })        
    except LoanNotRequiredError as e:
        return JSONResponse({"error": "open_access","message": "open_access"})
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post('/upload', status_code=status.HTTP_200_OK)
async def upload(
    request: Request,
    openlibrary_edition: int = Form(
        ..., gt=0, description="OpenLibrary Edition ID (must be a positive integer)"),
    encrypted: bool = Form(
        False, description="Set to true if the file is encrypted"),
    file: UploadFile = File(
        ..., description="The PDF or EPUB file to upload (max 50MB)")
):

    try:
        item = LennyAPI.add(
            openlibrary_edition=openlibrary_edition,
            files=[file],  # TODO expand to allow multiple
            uploader_ip=request.client.host,
            encrypt=encrypted,
        )
        return HTMLResponse(
            status_code=status.HTTP_200_OK,
            content="File uploaded successfully."
        )
    except UploaderNotAllowedError as e:
        raise HTTPException(status_code=503, details=str(e))
    except ItemExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except InvalidFileError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except DatabaseInsertError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except FileTooLargeError as e:
        raise HTTPException(status_code=413, detail=str(e))
    except S3UploadError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")


@router.get("/profile")
async def profile(session: str = Cookie(None)):
    """
    Returns the logged-in user's email if session is valid, else null.
    """
    email = session and auth.verify_session_cookie(session)
    loans = [{
        "loan_id": loan.id,
        "openlibrary_edition": getattr(loan, "openlibrary_edition", None),
        "borrowed_at": str(loan.created_at),
    } for loan in LennyAPI.get_borrowed_items(email)] if email else []
    return JSONResponse({
        "logged_in": bool(email),
        "email": email,
        "loans": loans,
        "loan_count": len(loans)
    })


@router.api_route("/logout", methods=["GET", "POST"])
async def logout(response: Response, session: str = Cookie(None)):
    response.delete_cookie(
        key="session",
        path="/",
        secure=True,
        samesite="Lax"
    )
    return {"success": True, "message": "Logged out successfully"}




@router.get("/oauth/implicit")
async def oauth_implicit(request: Request):
    """
    Returns the OPDS Authentication Document (JSON) describing the implicit flow.
    """
    return Response(
        content=json.dumps(LennyDataProvider.get_authentication_document()),
        media_type="application/opds-authentication+json"
    )

@router.api_route("/oauth/authorize", methods=["GET", "POST"])
async def oauth_authorize(
    request: Request, 
    response: Response,
    redirect_uri: Optional[str] = None,
    client_id: Optional[str] = None,
    state: Optional[str] = None
):
    """
    Handles the authorization request.
    If logged in, redirects to redirect_uri with access_token in fragment.
    If not logged in, handles OTP flow directly.
    """
    session = request.cookies.get("session")
    email = auth.verify_session_cookie(session)

    if email:
        body = await _parse_request_body(request)
        redirect_uri = redirect_uri or body.get("redirect_uri") or "opds://authorize/"
        state = state or body.get("state")
        
        fragment = _build_oauth_fragment(session, state)
        return RedirectResponse(url=f"{redirect_uri}#{urlencode(fragment)}", status_code=303)

    client_ip = request.client.host
    body = await _parse_request_body(request)
    req_params = dict(request.query_params)
    
    post_email = body.get("email")
    post_otp = body.get("otp")
    
    current_redirect_uri = body.get("redirect_uri") or req_params.get("redirect_uri") or "opds://authorize/"
    current_state = body.get("state") or req_params.get("state")
    current_client_id = body.get("client_id") or req_params.get("client_id")

    post_url = "/v1/api/oauth/authorize"
    if current_redirect_uri != "opds://authorize/":
        post_url += f"?redirect_uri={quote(current_redirect_uri, safe='')}"
    if current_state:
        post_url += f"&state={quote(current_state, safe='')}"
    
    context = {
        "request": request,
        "redirect_uri": current_redirect_uri,
        "state": current_state,
        "client_id": current_client_id,
        "post_url": post_url,
        "next": current_redirect_uri,
        "book_id": "oauth",
        "action": "oauth"
    }

    if request.method == "POST" and post_email and post_otp:
        session_cookie = auth.OTP.authenticate(post_email, post_otp, client_ip)
        if not session_cookie:
            context["error"] = "Authentication failed. Invalid OTP."
            context["email"] = post_email
            return request.app.templates.TemplateResponse("otp_redeem.html", context)
        
        fragment = _build_oauth_fragment(session_cookie, current_state)
        
        if current_redirect_uri.startswith("opds://"):
            success_context = {
                "request": request,
                "email": post_email,
                "auth_doc_id": LennyAPI.make_url("/v1/api/oauth/implicit"),
                "access_token": session_cookie,
                "expires_in": auth.COOKIE_TTL,
                "state": current_state
            }
            response = request.app.templates.TemplateResponse("oauth_success.html", success_context)
        else:
            response = RedirectResponse(
                url=f"{current_redirect_uri}#{urlencode(fragment)}",
                status_code=303
            )
        
        return _set_session_cookie(response, session_cookie)

    if request.method == "POST" and post_email:
        try:
            auth.OTP.issue(post_email, client_ip)
            context["email"] = post_email
            return request.app.templates.TemplateResponse("otp_redeem.html", context)
        except Exception:
            context["error"] = "Failed to issue OTP. Please try again."
            return request.app.templates.TemplateResponse("otp_issue.html", context)

    return request.app.templates.TemplateResponse("otp_issue.html", context)
