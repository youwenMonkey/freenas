import dateutil
import dateutil.parser
import os
import random
import re

from middlewared.async_validators import validate_country
from middlewared.schema import accepts, Dict, Int, List, Patch, Ref, Str
from middlewared.service import CRUDService, private, ValidationErrors
from middlewared.validators import Email, IpAddress, Range, ShouldBe
from OpenSSL import crypto, SSL
from cryptography.hazmat.primitives.asymmetric import ec


CA_TYPE_EXISTING = 0x01
CA_TYPE_INTERNAL = 0x02
CA_TYPE_INTERMEDIATE = 0x04
CERT_TYPE_EXISTING = 0x08
CERT_TYPE_INTERNAL = 0x10
CERT_TYPE_CSR = 0x20

CERT_ROOT_PATH = '/etc/certificates'
CERT_CA_ROOT_PATH = '/etc/certificates/CA'
RE_CERTIFICATE = re.compile(r"(-{5}BEGIN[\s\w]+-{5}[^-]+-{5}END[\s\w]+-{5})+", re.M | re.S)


def get_cert_info_from_data(data):
    cert_info_keys = ['key_length', 'country', 'state', 'city', 'organization', 'common',
                      'san', 'serial', 'email', 'lifetime', 'digest_algorithm']
    return {key: data.get(key) for key in cert_info_keys}


async def validate_cert_name(middleware, cert_name, datastore, verrors, name):
    certs = await middleware.call(
        'datastore.query',
        datastore,
        [('cert_name', '=', cert_name)]
    )
    if certs:
        verrors.add(
            name,
            'A certificate with this name already exists'
        )

    if cert_name in ("external", "self-signed", "external - signature pending"):
        verrors.add(
            name,
            f'{cert_name} is a reserved internal keyword for Certificate Management'
        )
    reg = re.search(r'^[a-z0-9_\-]+$', cert_name or '', re.I)
    if not reg:
        verrors.add(
            name,
            'Use alphanumeric characters, "_" and "-".'
        )


def _set_required(name):
    def set_r(attr):
        attr.required = True
    return {'name': name, 'method': set_r}


def load_private_key(buffer, passphrase=None):
    try:
        return crypto.load_privatekey(
            crypto.FILETYPE_PEM,
            buffer,
            passphrase=passphrase.encode() if passphrase else None
        )
    except crypto.Error:
        return None


def export_private_key(buffer, passphrase=None):
    key = load_private_key(buffer, passphrase)
    if key:
        return crypto.dump_privatekey(
            crypto.FILETYPE_PEM,
            key,
            passphrase=passphrase.encode() if passphrase else None
        ).decode()


def generate_key(key_length):
    k = crypto.PKey()
    k.generate_key(crypto.TYPE_RSA, key_length)
    return k


async def _validate_common_attributes(middleware, data, verrors, schema_name):

    def _validate_certificate_with_key(certificate, private_key, schema_name, verrors):
        if (
                (certificate and private_key) and
                all(k not in verrors for k in (f'{schema_name}.certificate', f'{schema_name}.privatekey'))
        ):
            public_key_obj = crypto.load_certificate(crypto.FILETYPE_PEM, certificate)
            private_key_obj = load_private_key(private_key, passphrase)

            try:
                context = SSL.Context(SSL.TLSv1_2_METHOD)
                context.use_certificate(public_key_obj)
                context.use_privatekey(private_key_obj)
                context.check_privatekey()
            except SSL.Error as e:
                verrors.add(
                    f'{schema_name}.privatekey',
                    f'Private key does not match certificate: {e}'
                )

    country = data.get('country')
    if country:
        await validate_country(middleware, country, verrors, f'{schema_name}.country')

    certificate = data.get('certificate')
    if certificate:
        matches = RE_CERTIFICATE.findall(certificate)

        if not matches or not await middleware.call('certificate.load_certificate', certificate):
            verrors.add(
                f'{schema_name}.certificate',
                'Not a valid certificate'
            )

    private_key = data.get('privatekey')
    passphrase = data.get('passphrase')
    if private_key:
        private_key_obj = load_private_key(private_key, passphrase)
        if not private_key_obj:
            verrors.add(
                f'{schema_name}.privatekey',
                'Please provide a valid private key with matching passphrase ( if any )'
            )
        elif (
                'create' in schema_name and private_key_obj.bits() < 1024 and not isinstance(
                    private_key_obj.to_cryptography_key(), ec.EllipticCurvePrivateKey
                )
        ):
            # When a cert/ca is being created, we disallow keys with size less then 1024
            # Update is allowed for now for keeping compatibility with very old cert/keys
            # We do not do this check for any EC based key
            verrors.add(
                f'{schema_name}.privatekey',
                'Please provide a key with size greater than or equal to 1024'
            )

    key_length = data.get('key_length')
    if key_length:
        if key_length not in [1024, 2048, 4096]:
            verrors.add(
                f'{schema_name}.key_length',
                'Key length must be a valid value ( 1024, 2048, 4096 )'
            )

    signedby = data.get('signedby')
    if signedby:
        valid_signing_ca = await middleware.call(
            'certificateauthority.query',
            [
                ('certificate', '!=', None),
                ('privatekey', '!=', None),
                ('certificate', '!=', ''),
                ('privatekey', '!=', ''),
                ('id', '=', signedby)
            ],
        )

        if not valid_signing_ca:
            verrors.add(
                f'{schema_name}.signedby',
                'Please provide a valid signing authority'
            )

    await middleware.run_in_thread(
        _validate_certificate_with_key, certificate, private_key, schema_name, verrors
    )


class CertificateService(CRUDService):

    class Config:
        datastore = 'system.certificate'
        datastore_extend = 'certificate.cert_extend'
        datastore_prefix = 'cert_'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.map_functions = {
            'CERTIFICATE_CREATE_INTERNAL': self.__create_internal,
            'CERTIFICATE_CREATE_IMPORTED': self.__create_imported_certificate,
            'CERTIFICATE_CREATE': self.__create_certificate,
            'CERTIFICATE_CREATE_CSR': self.__create_csr
        }

    @private
    async def cert_extend(self, cert):
        """Extend certificate with some useful attributes."""

        if cert.get('signedby'):

            # We query for signedby again to make sure it's keys do not have the "cert_" prefix and it has gone through
            # the cert_extend method

            cert['signedby'] = await self.middleware.call(
                'datastore.query',
                'system.certificateauthority',
                [('id', '=', cert['signedby']['id'])],
                {
                    'prefix': 'cert_',
                    'extend': 'certificate.cert_extend',
                    'get': True
                }
            )

        # convert san to list
        cert['san'] = (cert.pop('san', '') or '').split()
        if cert['serial'] is not None:
            cert['serial'] = int(cert['serial'])

        if cert['type'] in (
                CA_TYPE_EXISTING, CA_TYPE_INTERNAL, CA_TYPE_INTERMEDIATE
        ):
            root_path = CERT_CA_ROOT_PATH
        else:
            root_path = CERT_ROOT_PATH
        cert['root_path'] = root_path
        cert['certificate_path'] = os.path.join(
            root_path, '{0}.crt'.format(cert['name'])
        )
        cert['privatekey_path'] = os.path.join(
            root_path, '{0}.key'.format(cert['name'])
        )
        cert['csr_path'] = os.path.join(
            root_path, '{0}.csr'.format(cert['name'])
        )

        def cert_issuer(cert):
            issuer = None
            if cert['type'] in (CA_TYPE_EXISTING, CERT_TYPE_EXISTING):
                issuer = "external"
            elif cert['type'] == CA_TYPE_INTERNAL:
                issuer = "self-signed"
            elif cert['type'] in (CERT_TYPE_INTERNAL, CA_TYPE_INTERMEDIATE):
                issuer = cert['signedby']
            elif cert['type'] == CERT_TYPE_CSR:
                issuer = "external - signature pending"
            return issuer

        cert['issuer'] = cert_issuer(cert)

        cert['chain_list'] = []
        if cert['chain']:
            certs = RE_CERTIFICATE.findall(cert['certificate'])
        else:
            certs = [cert['certificate']]
            signing_CA = cert['issuer']
            # Recursively get all internal/intermediate certificates
            # FIXME: NONE HAS BEEN ADDED IN THE FOLLOWING CHECK FOR CSR'S WHICH HAVE BEEN SIGNED BY A CA
            while signing_CA not in ["external", "self-signed", "external - signature pending", None]:
                certs.append(signing_CA['certificate'])
                signing_CA['issuer'] = cert_issuer(signing_CA)
                signing_CA = signing_CA['issuer']

        cert_obj = None
        try:
            for c in certs:
                # XXX Why load certificate if we are going to dump it right after?
                # Maybe just to verify its integrity?
                # Logic copied from freenasUI
                cert_obj = crypto.load_certificate(crypto.FILETYPE_PEM, c)
                cert['chain_list'].append(
                    crypto.dump_certificate(crypto.FILETYPE_PEM, cert_obj).decode()
                )
        except Exception:
            self.logger.debug('Failed to load certificate {0}'.format(cert['name']), exc_info=True)

        try:
            if cert['privatekey']:
                key_obj = crypto.load_privatekey(crypto.FILETYPE_PEM, cert['privatekey'])
                cert['privatekey'] = crypto.dump_privatekey(crypto.FILETYPE_PEM, key_obj).decode()
        except Exception:
            self.logger.debug('Failed to load privatekey {0}'.format(cert['name']), exc_info=True)

        try:
            if cert['CSR']:
                csr_obj = crypto.load_certificate_request(crypto.FILETYPE_PEM, cert['CSR'])
                cert['CSR'] = crypto.dump_certificate_request(crypto.FILETYPE_PEM, csr_obj).decode()
        except Exception:
            self.logger.debug('Failed to load csr {0}'.format(cert['name']), exc_info=True)

        cert['internal'] = 'NO' if cert['type'] in (CA_TYPE_EXISTING, CERT_TYPE_EXISTING) else 'YES'

        obj = None
        # date not applicable for CSR
        cert['from'] = None
        cert['until'] = None
        if cert['type'] == CERT_TYPE_CSR:
            obj = csr_obj
        elif cert_obj:
            obj = crypto.load_certificate(crypto.FILETYPE_PEM, cert['certificate'])
            notBefore = obj.get_notBefore()
            t1 = dateutil.parser.parse(notBefore)
            t2 = t1.astimezone(dateutil.tz.tzlocal())
            cert['from'] = t2.ctime()

            notAfter = obj.get_notAfter()
            t1 = dateutil.parser.parse(notAfter)
            t2 = t1.astimezone(dateutil.tz.tzlocal())
            cert['until'] = t2.ctime()

        if obj:
            cert['DN'] = '/' + '/'.join([
                '%s=%s' % (c[0].decode(), c[1].decode())
                for c in obj.get_subject().get_components()
            ])

        return cert

    # HELPER METHODS

    @private
    @accepts(
        Str('certificate', required=True)
    )
    def load_certificate(self, certificate):
        try:
            cert = crypto.load_certificate(
                crypto.FILETYPE_PEM,
                certificate
            )
        except crypto.Error:
            return {}
        else:
            cert_info = {
                'country': cert.get_subject().C,
                'state': cert.get_subject().ST,
                'city': cert.get_subject().L,
                'organization': cert.get_subject().O,
                'common': cert.get_subject().CN,
                'san': cert.get_subject().subjectAltName,
                'email': cert.get_subject().emailAddress,
                'serial': cert.get_serial_number()
            }

            signature_algorithm = cert.get_signature_algorithm().decode()
            m = re.match('^(.+)[Ww]ith', signature_algorithm)
            if m:
                cert_info['digest_algorithm'] = m.group(1).upper()

            return cert_info

    @private
    async def get_fingerprint_of_cert(self, certificate_id):
        certificate_list = await self.query(filters=[('id', '=', certificate_id)])
        if len(certificate_list) == 0:
            return None
        else:
            return await self.middleware.run_in_thread(
                self.fingerprint,
                certificate_list[0]['certificate']
            )

    @private
    @accepts(
        Str('cert_certificate', required=True)
    )
    def fingerprint(self, cert_certificate):
        # getting fingerprint of certificate
        try:
            certificate = crypto.load_certificate(
                crypto.FILETYPE_PEM,
                cert_certificate
            )
        except Exception:
            return None
        else:
            return certificate.digest('sha1').decode()

    @private
    async def san_to_string(self, san_list):
        # TODO: ADD MORE TYPES WRT RFC'S
        san_string = ''
        ip_validator = IpAddress()
        for count, san in enumerate(san_list or []):
            try:
                ip_validator(san)
            except ShouldBe:
                san_string += f'DNS: {san}, '
            else:
                san_string += f'IP: {san}, '
        return san_string[:-2] if san_list else ''

    @private
    @accepts(
        Dict(
            'certificate_cert_info',
            Int('key_length'),
            Int('serial', required=False),
            Int('lifetime', required=True),
            Str('country', required=True),
            Str('state', required=True),
            Str('city', required=True),
            Str('organization', required=True),
            Str('common', required=True),
            Str('email', validators=[Email()], required=True),
            Str('digest_algorithm', enum=['SHA1', 'SHA224', 'SHA256', 'SHA384', 'SHA512']),
            List('san', items=[Str('san')]),
            register=True
        )
    )
    def create_certificate(self, cert_info):

        cert_info['san'] = self.middleware.call_sync(
            'certificate.san_to_string',
            cert_info.pop('san', [])
        )

        cert = crypto.X509()
        cert.get_subject().C = cert_info['country']
        cert.get_subject().ST = cert_info['state']
        cert.get_subject().L = cert_info['city']
        cert.get_subject().O = cert_info['organization']
        cert.get_subject().CN = cert_info['common']
        # Add subject alternate name in addition to CN

        if cert_info['san']:
            cert.add_extensions([crypto.X509Extension(
                b"subjectAltName", False, cert_info['san'].encode()
            )])
            cert.get_subject().subjectAltName = cert_info['san']
        cert.get_subject().emailAddress = cert_info['email']

        serial = cert_info.get('serial')
        if serial is not None:
            cert.set_serial_number(serial)

        cert.gmtime_adj_notBefore(0)
        cert.gmtime_adj_notAfter(cert_info['lifetime'] * (60 * 60 * 24))

        cert.set_issuer(cert.get_subject())
        # Setting it to '2' actually results in a v3 cert
        # openssl's cert x509 versions are zero-indexed!
        # see: https://www.ietf.org/rfc/rfc3280.txt
        cert.set_version(2)
        return cert

    @private
    @accepts(
        Patch(
            'certificate_cert_info', 'certificate_signing_request',
            ('rm', {'name': 'lifetime'})
        )
    )
    def create_certificate_signing_request(self, cert_info):

        cert_info['san'] = self.middleware.call_sync(
            'certificate.san_to_string',
            cert_info.pop('san', [])
        )

        key = generate_key(cert_info['key_length'])

        req = crypto.X509Req()
        req.get_subject().C = cert_info['country']
        req.get_subject().ST = cert_info['state']
        req.get_subject().L = cert_info['city']
        req.get_subject().O = cert_info['organization']
        req.get_subject().CN = cert_info['common']

        if cert_info['san']:
            req.add_extensions(
                [crypto.X509Extension(b"subjectAltName", False, cert_info['san'].encode())])
            req.get_subject().subjectAltName = cert_info['san']
        req.get_subject().emailAddress = cert_info['email']

        req.set_pubkey(key)
        req.sign(key, cert_info['digest_algorithm'])

        return (req, key)

    @private
    async def validate_common_attributes(self, data, schema_name):
        verrors = ValidationErrors()

        await _validate_common_attributes(self.middleware, data, verrors, schema_name)

        return verrors

    # CREATE METHODS FOR CREATING CERTIFICATES
    # "do_create" IS CALLED FIRST AND THEN BASED ON THE TYPE OF THE CERTIFICATE WHICH IS TO BE CREATED THE
    # APPROPRIATE METHOD IS CALLED
    # FOLLOWING TYPES ARE SUPPORTED
    # CREATE_TYPE ( STRING )      - METHOD CALLED
    # CERTIFICATE_CREATE_INTERNAL - __create_internal
    # CERTIFICATE_CREATE_IMPORTED - __create_imported_certificate
    # CERTIFICATE_CREATE          - __create_certificate
    # CERTIFICATE_CREATE_CSR      - __create_csr

    @accepts(
        Dict(
            'certificate_create',
            Int('signedby'),
            Int('key_length'),
            Int('type'),
            Int('lifetime'),
            Int('serial', validators=[Range(min=1)]),
            Str('certificate'),
            Str('city'),
            Str('common'),
            Str('country'),
            Str('CSR'),
            Str('email', validators=[Email()]),
            Str('name', required=True),
            Str('organization'),
            Str('passphrase'),
            Str('privatekey'),
            Str('state'),
            Str('create_type', enum=[
                'CERTIFICATE_CREATE_INTERNAL', 'CERTIFICATE_CREATE_IMPORTED',
                'CERTIFICATE_CREATE', 'CERTIFICATE_CREATE_CSR'], required=True),
            Str('digest_algorithm', enum=['SHA1', 'SHA224', 'SHA256', 'SHA384', 'SHA512']),
            List('san', items=[Str('san')]),
            register=True
        )
    )
    async def do_create(self, data):

        verrors = await self.validate_common_attributes(data, 'certificate_create')

        await validate_cert_name(
            self.middleware, data['name'], self._config.datastore,
            verrors, 'certificate_create.name'
        )

        if verrors:
            raise verrors

        data = await self.middleware.run_in_thread(
            self.map_functions[data.pop('create_type')],
            data
        )

        data['san'] = ' '.join(data.pop('san', []) or [])

        pk = await self.middleware.call(
            'datastore.insert',
            self._config.datastore,
            data,
            {'prefix': self._config.datastore_prefix}
        )

        await self.middleware.call(
            'service.start',
            'ix-ssl',
            {'onetime': False}
        )

        return await self._get_instance(pk)

    @accepts(
        Patch(
            'certificate_create_internal', 'certificate_create_csr',
            ('rm', {'name': 'signedby'}),
            ('rm', {'name': 'lifetime'})
        )
    )
    def __create_csr(self, data):
        # no signedby, lifetime attributes required
        cert_info = get_cert_info_from_data(data)
        cert_info.pop('lifetime')

        data['type'] = CERT_TYPE_CSR

        req, key = self.create_certificate_signing_request(cert_info)

        data['CSR'] = crypto.dump_certificate_request(crypto.FILETYPE_PEM, req)
        data['privatekey'] = crypto.dump_privatekey(crypto.FILETYPE_PEM, key)

        return data

    @accepts(
        Patch(
            'certificate_create', 'create_certificate',
            ('edit', _set_required('certificate')),
            ('edit', _set_required('privatekey')),
            ('edit', _set_required('type')),
            ('rm', {'name': 'create_type'})
        )
    )
    def __create_certificate(self, data):

        for k, v in self.load_certificate(data['certificate']).items():
            data[k] = v

        return data

    @accepts(
        Patch(
            'certificate_create', 'certificate_create_imported',
            ('edit', _set_required('certificate')),
            ('edit', _set_required('privatekey')),
            ('rm', {'name': 'create_type'})
        )
    )
    def __create_imported_certificate(self, data):
        data['type'] = CERT_TYPE_EXISTING

        data = self.__create_certificate(data)

        data['chain'] = True if len(RE_CERTIFICATE.findall(data['certificate'])) > 1 else False

        if 'passphrase' in data:
            data['privatekey'] = export_private_key(
                data['privatekey'],
                data['passphrase']
            )

        data.pop('passphrase', None)

        return data

    @accepts(
        Patch(
            'certificate_create', 'certificate_create_internal',
            ('edit', _set_required('key_length')),
            ('edit', _set_required('digest_algorithm')),
            ('edit', _set_required('lifetime')),
            ('edit', _set_required('country')),
            ('edit', _set_required('state')),
            ('edit', _set_required('city')),
            ('edit', _set_required('organization')),
            ('edit', _set_required('email')),
            ('edit', _set_required('common')),
            ('edit', _set_required('signedby')),
            ('rm', {'name': 'create_type'}),
            register=True
        )
    )
    def __create_internal(self, data):

        cert_info = get_cert_info_from_data(data)
        data['type'] = CERT_TYPE_INTERNAL

        signing_cert = self.middleware.call_sync(
            'certificateauthority.query',
            [('id', '=', data['signedby'])],
            {'get': True}
        )

        public_key = generate_key(data['key_length'])
        signkey = load_private_key(signing_cert['privatekey'])

        cert = self.middleware.call_sync('certificate.create_certificate', cert_info)
        cert.set_pubkey(public_key)
        cacert = crypto.load_certificate(crypto.FILETYPE_PEM, signing_cert['certificate'])
        cert.set_issuer(cacert.get_subject())
        cert.add_extensions([
            crypto.X509Extension(b"subjectKeyIdentifier", False, b"hash", subject=cert),
        ])

        cert_serial = self.middleware.call_sync(
            'certificateauthority.get_serial_for_certificate',
            data['signedby']
        )

        cert.set_serial_number(cert_serial)
        cert.sign(signkey, data['digest_algorithm'])

        data['certificate'] = crypto.dump_certificate(crypto.FILETYPE_PEM, cert)
        data['privatekey'] = crypto.dump_privatekey(crypto.FILETYPE_PEM, public_key)
        data['serial'] = cert_serial

        return data

    @accepts(
        Int('id', required=True),
        Dict(
            'certificate_update',
            Str('name'),
            Str('certificate')
        )
    )
    async def do_update(self, id, data):
        old = await self._get_instance(id)
        # signedby is changed back to integer from a dict
        old['signedby'] = old['signedby']['id'] if old.get('signedby') else None

        new = old.copy()

        new.update(data)

        verrors = ValidationErrors()

        # TODO: THIS WILL BE REMOVED IN 11.3 - WO DON'T WANT TO ALLOW UPDATES TO THE CERTIFICATE FIELD
        if new['type'] != CERT_TYPE_CSR and data.get('certificate'):
            verrors.add(
                'certificate_update.certificate',
                'Certificate field cannot be updated'
            )
        elif data.get('certificate'):
            verrors = await self.validate_common_attributes(new, 'certificate_update')

            if not verrors:
                new['type'] = CERT_TYPE_EXISTING

                new.update(
                    (await self.middleware.run_in_thread(self.load_certificate, new['certificate']))
                )

                new['chain'] = True if len(RE_CERTIFICATE.findall(new['certificate'])) > 1 else False

        if new['name'] != old['name']:

            await validate_cert_name(self.middleware, data['name'], self._config.datastore, verrors,
                                     'certificate_update.name')

        if verrors:
            raise verrors

        new['san'] = ' '.join(new.pop('san', []) or [])

        await self.middleware.call(
            'datastore.update',
            self._config.datastore,
            id,
            new,
            {'prefix': self._config.datastore_prefix}
        )

        await self.middleware.call(
            'service.start',
            'ix-ssl',
            {'onetime': False}
        )

        return await self._get_instance(id)

    @accepts(
        Int('id')
    )
    async def do_delete(self, id):
        response = await self.middleware.call(
            'datastore.delete',
            self._config.datastore,
            id
        )

        await self.middleware.call(
            'service.start',
            'ix-ssl',
            {'onetime': False}
        )
        return response


class CertificateAuthorityService(CRUDService):

    class Config:
        datastore = 'system.certificateauthority'
        datastore_extend = 'certificate.cert_extend'
        datastore_prefix = 'cert_'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.map_create_functions = {
            'CA_CREATE_INTERNAL': self.__create_internal,
            'CA_CREATE_IMPORTED': self.__create_imported_ca,
            'CA_CREATE_INTERMEDIATE': self.__create_intermediate_ca,
        }

    # HELPER METHODS

    @private
    async def validate_common_attributes(self, data, schema_name):
        verrors = ValidationErrors()

        await _validate_common_attributes(self.middleware, data, verrors, schema_name)

        return verrors

    @private
    async def get_serial_for_certificate(self, ca_id):

        ca_data = await self._get_instance(ca_id)

        if ca_data.get('signedby'):
            # Recursively call the same function for it's parent and let the function gather all serials in a chain
            return await self.get_serial_for_certificate(ca_data['signedby']['id'])
        else:

            async def cert_serials(ca_id):
                return [
                    data['serial'] for data in
                    await self.middleware.call(
                        'datastore.query',
                        'system.certificate',
                        [('signedby', '=', ca_id)],
                        {
                            'prefix': self._config.datastore_prefix,
                            'extend': self._config.datastore_extend
                        }
                    )
                ]

            ca_signed_certs = await cert_serials(ca_id)

            async def child_serials(ca_id):
                serials = []
                children = await self.middleware.call(
                    'datastore.query',
                    self._config.datastore,
                    [('signedby', '=', ca_id)],
                    {
                        'prefix': self._config.datastore_prefix,
                        'extend': self._config.datastore_extend
                    }
                )

                for child in children:
                    serials.extend((await child_serials(child['id'])))

                serials.extend((await cert_serials(ca_id)))
                serials.append((await self._get_instance(ca_id))['serial'])

                return serials

            ca_signed_certs.extend((await child_serials(ca_id)))

            # There is for a case when user might have old certs in the db whose serial value
            # isn't set in the db
            ca_signed_certs = list(filter(None, ca_signed_certs))

            if not ca_signed_certs:
                return int(
                    (await self._get_instance(ca_id))['serial'] or 0
                ) + 1
            else:
                return max(ca_signed_certs) + 1

    @private
    @accepts(
        Ref('certificate_cert_info')
    )
    def create_self_signed_CA(self, cert_info):

        key = generate_key(cert_info['key_length'])
        cert = self.middleware.call_sync('certificate.create_certificate', cert_info)
        cert.set_pubkey(key)
        cert.add_extensions([
            crypto.X509Extension(b"basicConstraints", True, b"CA:TRUE"),
            crypto.X509Extension(b"keyUsage", True, b"keyCertSign, cRLSign"),
            crypto.X509Extension(b"subjectKeyIdentifier", False, b"hash", subject=cert),
        ])
        serial = cert_info.get('serial')
        cert.set_serial_number(serial or 0o1)
        cert.sign(key, cert_info['digest_algorithm'])
        return (cert, key)

    def _set_enum(name):
        def set_enum(attr):
            attr.enum = ['CA_CREATE_INTERNAL', 'CA_CREATE_IMPORTED', 'CA_CREATE_INTERMEDIATE']
        return {'name': name, 'method': set_enum}

    # CREATE METHODS FOR CREATING CERTIFICATE AUTHORITIES
    # "do_create" IS CALLED FIRST AND THEN BASED ON THE TYPE OF CA WHICH IS TO BE CREATED, THE
    # APPROPRIATE METHOD IS CALLED
    # FOLLOWING TYPES ARE SUPPORTED
    # CREATE_TYPE ( STRING )      - METHOD CALLED
    # CA_CREATE_INTERNAL          - __create_internal
    # CA_CREATE_IMPORTED          - __create_imported_ca
    # CA_CREATE_INTERMEDIATE      - __create_intermediate_ca

    @accepts(
        Patch(
            'certificate_create', 'ca_create',
            ('edit', _set_enum('create_type')),
            register=True
        )
    )
    async def do_create(self, data):
        verrors = await self.validate_common_attributes(data, 'certificate_authority_create')

        await validate_cert_name(
            self.middleware, data['name'], self._config.datastore,
            verrors, 'certificate_authority_create.name'
        )

        if verrors:
            raise verrors

        data = await self.middleware.run_in_thread(
            self.map_create_functions[data.pop('create_type')],
            data
        )

        data['san'] = ' '.join(data.pop('san', []) or [])

        pk = await self.middleware.call(
            'datastore.insert',
            self._config.datastore,
            data,
            {'prefix': self._config.datastore_prefix}
        )

        await self.middleware.call(
            'service.start',
            'ix-ssl',
            {'onetime': False}
        )

        return await self._get_instance(pk)

    @accepts(
        Dict(
            'ca_sign_csr',
            Int('ca_id', required=True),
            Int('csr_cert_id', required=True),
            Str('name', required=True),
            register=True
        )
    )
    def ca_sign_csr(self, data):
        return self.__ca_sign_csr(data)

    @accepts(
        Ref('ca_sign_csr'),
        Str('schema_name', default='certificate_authority_update')
    )
    def __ca_sign_csr(self, data, schema_name):
        verrors = ValidationErrors()

        ca_data = self.middleware.call_sync(
            'certificateauthority.query',
            ([('id', '=', data['ca_id'])])
        )
        csr_cert_data = self.middleware.call_sync('certificate.query', [('id', '=', data['csr_cert_id'])])

        if not ca_data:
            verrors.add(
                f'{schema_name}.ca_id',
                f'No Certificate Authority found for id {data["ca_id"]}'
            )
        else:
            ca_data = ca_data[0]
            if not ca_data.get('privatekey'):
                verrors.add(
                    f'{schema_name}.ca_id',
                    'Please use a CA which has a private key assigned'
                )

        if not csr_cert_data:
            verrors.add(
                f'{schema_name}.csr_cert_id',
                f'No Certificate found for id {data["csr_cert_id"]}'
            )
        else:
            csr_cert_data = csr_cert_data[0]
            if not csr_cert_data.get('CSR'):
                verrors.add(
                    f'{schema_name}.csr_cert_id',
                    'No CSR has been filed by this certificate'
                )
            else:
                try:
                    csr = crypto.load_certificate_request(crypto.FILETYPE_PEM, csr_cert_data['CSR'])
                except crypto.Error:
                    verrors.add(
                        f'{schema_name}.csr_cert_id',
                        'CSR not valid'
                    )

        if verrors:
            raise verrors

        cert_info = crypto.load_certificate(crypto.FILETYPE_PEM, ca_data['certificate'])
        PKey = load_private_key(ca_data['privatekey'])

        serial = self.middleware.call_sync(
            'certificateauthority.get_serial_for_certificate',
            ca_data['id']
        )

        cert = crypto.X509()
        cert.set_serial_number(serial)
        cert.gmtime_adj_notBefore(0)
        cert.gmtime_adj_notAfter(86400 * 365 * 10)
        cert.set_issuer(cert_info.get_subject())
        cert.set_subject(csr.get_subject())
        cert.set_pubkey(csr.get_pubkey())
        cert.sign(PKey, ca_data['digest_algorithm'])

        new_cert = crypto.dump_certificate(crypto.FILETYPE_PEM, cert).decode()

        new_csr = {
            'type': CERT_TYPE_INTERNAL,
            'name': data['name'],
            'certificate': new_cert,
            'privatekey': csr_cert_data['privatekey'],
            'create_type': 'CERTIFICATE_CREATE',
            'signedby': ca_data['id']   # Is this the right step ? If a CA signs a CSR, should it be the signedby
        }                               # entity for that certificate

        new_csr_dict = self.middleware.call_sync(
            'certificate.create',
            new_csr
        )

        return new_csr_dict

    @accepts(
        Patch(
            'ca_create_interal', 'ca_create_intermediate',
            ('add', {'name': 'signedby', 'type': 'int', 'required': True}),
        ),
    )
    def __create_intermediate_ca(self, data):

        signing_cert = self.middleware.call_sync(
            'certificateauthority._get_instance',
            data['signedby']
        )

        serial = self.middleware.call_sync(
            'certificateauthority.get_serial_for_certificate',
            signing_cert['id']
        )

        data['type'] = CA_TYPE_INTERMEDIATE
        cert_info = get_cert_info_from_data(data)

        publickey = generate_key(data['key_length'])
        signkey = load_private_key(signing_cert['privatekey'])

        cert = self.middleware.call_sync('certificate.create_certificate', cert_info)
        cert.set_pubkey(publickey)
        cacert = crypto.load_certificate(crypto.FILETYPE_PEM, signing_cert['certificate'])
        cert.set_issuer(cacert.get_subject())
        cert.add_extensions([
            crypto.X509Extension(b"basicConstraints", True, b"CA:TRUE, pathlen:0"),
            crypto.X509Extension(b"keyUsage", True, b"keyCertSign, cRLSign"),
            crypto.X509Extension(b"subjectKeyIdentifier", False, b"hash", subject=cert),
        ])

        cert.set_serial_number(serial)
        data['serial'] = serial
        cert.sign(signkey, data['digest_algorithm'])

        data['certificate'] = crypto.dump_certificate(crypto.FILETYPE_PEM, cert)
        data['privatekey'] = crypto.dump_privatekey(crypto.FILETYPE_PEM, publickey)

        return data

    @accepts(
        Patch(
            'ca_create', 'ca_create_imported',
            ('edit', _set_required('certificate')),
            ('rm', {'name': 'create_type'}),
        )
    )
    def __create_imported_ca(self, data):
        data['type'] = CA_TYPE_EXISTING
        data['chain'] = True if len(RE_CERTIFICATE.findall(data['certificate'])) > 1 else False

        for k, v in self.middleware.call_sync('certificate.load_certificate', data['certificate']).items():
            data[k] = v

        if all(k in data for k in ('passphrase', 'privatekey')):
            data['privatekey'] = export_private_key(
                data['privatekey'],
                data['passphrase']
            )

        data.pop('passphrase', None)

        return data

    @accepts(
        Patch(
            'ca_create', 'ca_create_interal',
            ('edit', _set_required('key_length')),
            ('edit', _set_required('digest_algorithm')),
            ('edit', _set_required('lifetime')),
            ('edit', _set_required('country')),
            ('edit', _set_required('state')),
            ('edit', _set_required('city')),
            ('edit', _set_required('organization')),
            ('edit', _set_required('email')),
            ('edit', _set_required('common')),
            ('rm', {'name': 'create_type'}),
            register=True
        )
    )
    def __create_internal(self, data):
        cert_info = get_cert_info_from_data(data)
        cert_info['serial'] = random.getrandbits(24)
        (cert, key) = self.create_self_signed_CA(cert_info)

        data['type'] = CA_TYPE_INTERNAL
        data['certificate'] = crypto.dump_certificate(crypto.FILETYPE_PEM, cert)
        data['privatekey'] = crypto.dump_privatekey(crypto.FILETYPE_PEM, key)
        data['serial'] = cert_info['serial']

        return data

    @accepts(
        Int('id', required=True),
        Dict(
            'ca_update',
            Int('ca_id'),
            Int('csr_cert_id'),
            Str('create_type', enum=['CA_SIGN_CSR']),
            Str('name'),
        )
    )
    async def do_update(self, id, data):

        if data.pop('create_type', '') == 'CA_SIGN_CSR':
            data['ca_id'] = id
            return await self.middleware.run_in_thread(
                self.__ca_sign_csr,
                data,
                'certificate_authority_update'
            )

        old = await self._get_instance(id)
        # signedby is changed back to integer from a dict
        old['signedby'] = old['signedby']['id'] if old.get('signedby') else None

        new = old.copy()
        new.update(data)

        verrors = ValidationErrors()

        if new['name'] != old['name']:
            await validate_cert_name(self.middleware, data['name'], self._config.datastore, verrors,
                                     'certificate_authority_update.name')

            if verrors:
                raise verrors

            new['san'] = ' '.join(new.pop('san', []) or [])

            await self.middleware.call(
                'datastore.update',
                self._config.datastore,
                id,
                new,
                {'prefix': self._config.datastore_prefix}
            )

            await self.middleware.call(
                'service.start',
                'ix-ssl',
                {'onetime': False}
            )

        return await self._get_instance(id)

    @accepts(
        Int('id')
    )
    async def do_delete(self, id):
        response = await self.middleware.call(
            'datastore.delete',
            self._config.datastore,
            id
        )

        await self.middleware.call(
            'service.start',
            'ix-ssl',
            {'onetime': False}
        )
        return response
