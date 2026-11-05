"""Synthetic Alipay RSA messages; signing is independent of production helpers."""

import base64
from urllib.parse import parse_qsl, urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
import pytest

APP_ID = '2021000000000000'
SELLER_ID = '2088000000000000'


@pytest.fixture(scope='module')
def alipay_keys():
    def pair():
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return {'merchant': pair(), 'provider': pair()}


def settings(keys):
    return {
        'app_id': APP_ID, 'seller_id': SELLER_ID,
        'private_key_pem': keys['merchant'].private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode(),
        'alipay_public_key_pem': keys['provider'].public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode(),
    }


def notify(keys, order_id='order_local', **overrides):
    form = dict(app_id=APP_ID, seller_id=SELLER_ID, out_trade_no=order_id,
                trade_no='202610280000001', trade_status='TRADE_SUCCESS',
                total_amount='9.99', sign_type='RSA2')
    form.update(overrides)
    content = '&'.join(f'{key}={value}' for key, value in sorted(form.items())
                       if key not in {'sign', 'sign_type'} and value not in ('', None))
    form['sign'] = base64.b64encode(keys['provider'].sign(
        content.encode(), padding.PKCS1v15(), hashes.SHA256())).decode()
    return form


def verify_redirect(keys, url):
    params = dict(parse_qsl(urlsplit(url).query))
    signature = base64.b64decode(params.pop('sign'), validate=True)
    # Page-pay requests sign every supplied field, INCLUDING sign_type.
    content = '&'.join(f'{key}={value}' for key, value in sorted(params.items()))
    keys['merchant'].public_key().verify(
        signature, content.encode(), padding.PKCS1v15(), hashes.SHA256())
    return params
