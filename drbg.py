
import hashlib
import hmac
import math
import re
import os
import sys

try:
    import Crypto.Cipher.AES
    from Crypto.Util.strxor import strxor
except ImportError:
    WITH_CRYPTO = False

PY3 = sys.version_info.major == 3

# List of supported hash algorithms.
DIGESTS = ['sha1', 'sha224', 'sha256', 'sha384', 'sha512']

# List of supported ciphers for CTR_DRBG
CIPHERS = ['aes128', 'aes192', 'aes256']

# Maximum length for entropy input, nonces, personaliation strings
# and additional input. (in bytes).
MAX_ENTROPY_LENGTH = 2**21

# Number of generate calls before a reseed is required.
RESEED_INTERVAL = 2 # 2**24


class Error (Exception):
    pass


class UnknownAlgorithm (Exception):
    pass


class ReseedRequired (Exception):
    pass


def bytes2long(b):
    if PY3:
        num = int.from_bytes(b, 'big')
    else:
        if isinstance(b, bytes):
            b = bytearray(b)
        length = len(b) - 1
        num = sum(_ * 256**(length - exp) for exp, _ in enumerate(b))

    return num


def long2bytes(l):
    as_hex = hex(l)[2:]

    if as_hex[-1] == 'L':
        as_hex = as_hex[:-1]

    if len(as_hex) % 2 != 0:
        as_hex = '0{}'.format(as_hex)

    if PY3:
        return bytes.fromhex(as_hex)
    else:
        return bytes(bytearray.fromhex(as_hex))


def bytepad(b, L):
    ''' Pad `b` to length `L` by prepending \x00's '''
    if len(b) < L:
        delta = L - len(b)
        b = (b'\x00' * delta) + b
    return b


class RandomByteGenerator (object):
    ''' High level wrapper for DRBG classes.

    :param drbg: The DRBG to use for generating random bytes.
    :type drbg: :class:`DRBG`.

    Objects of this class automatically reseed the underlying DRBG when
    required. The DRBG is reseeded with 32 bytes from the system random
    number generator.
    '''

    def __init__(self, drbg):
        self.drbg = drbg
        self._buf = b''
        self._buf_index = 0

    def generate (self, count=None):
        ''' Returns `count` random bytes, if `count` is None then
            the `outlen` number of bytes are returned.

        :param count: The number of bytes to return.
        '''
        if count is None:
            count = self.drbg.outlen // 8
        return self._get_bytes(count)

    def _get_bytes (self, count):
        buf = b''
        max_request = self.drbg.max_request_size

        while count > 0:
            count_ = max_request if count > max_request else count
            try:
                buf += self.drbg.generate(count_)
            except ReseedRequired:
                self.drbg.reseed(os.urandom(32))
            else:
                count -= count_

        return buf

    def __next__ (self):
        if self._buf_index == 0:
            self._buf = self.generate()
            self._buf_index = len(self._buf)
        self._buf_index -= 1
        return self._buf[self._buf_index]

    def __iter__ (self):
        return self



class DRBG (object):
    ''' Deterministic Random Bit Generator base class.

    :param entropy: A string of random bytes, minimum length is
                    algorithm specific.
    :type entropy: :class:`bytes` or :class:`bytearray`.
    :param data: Optional personalization string.
    :type data: :class:`bytes` or :class:`bytearray`.

    .. warning:: Supplying the DRBG with poor quality values for `entropy`
                 or `nonce` will result in low quality output. A good
                 cross-platform source of randomness is `os.urandom()`.
    '''

    def __init__(self, entropy, data=None):
        if not isinstance(entropy, (bytes, bytearray)):
            raise TypeError(('Expected bytes or bytearray for entropy'
                             'got {}').format(type(entropy.__name__)))

        if data is not None and not isinstance(data, (bytes, bytearray)):
            raise TypeError(('Expected bytes or bytearray for data'
                             'got {} ').format(type(data).__name__))

        self.reseed_counter = 1

    def generate(self, count, data=None):
        ''' Generate the next `count` random bytes.

        :param count: The number of bytes to return.
        :param data: Optional addition data.
        :type data: :class:`bytes` or :class:`bytearray`.

        :returns: :class:`bytes`.

        :raises: A :class:`ValueError` is raised if `count` is out of range,
                 maximum allowed number is algorithm dependent and specified
                 in SP 800-90A. A :class:`ReseedRequired` is raised if
                 the generator should be reseeded.
        '''
        if not 0 < count < self.max_request_size:
            raise ValueError('Count out of range.')

        if data is not None and not isinstance(data, (bytes, bytearray)):
            raise TypeError('Expected bytes or bytearray for data.')

        if data is not None and len(data) > MAX_ENTROPY_LENGTH:
            raise ValueError('Too much data.')

        if self.reseed_counter > RESEED_INTERVAL:
            raise ReseedRequired()

        out = self._generate(count, data)
        self.reseed_counter += 1
        return out

    def reseed(self, entropy, data=None):
        ''' Reseed the DRBG.

        :param entropy: A string of random bytes, minimum length is
                        algorithm specific.
        :type entropy: :class:`bytes` or :class:`bytearray`.
        :param data: Optional personalization string.
        :type data: :class:`bytes` or :class:`bytearray`.
        '''
        if not isinstance(entropy, (bytes, bytearray)):
            raise TypeError(('Expected bytes or bytearray for entropy'
                             'got {}').format(type(entropy.__name__)))

        if data is not None and not isinstance(data, (bytes, bytearray)):
            raise TypeError(('Expected bytes or bytearray for data '
                             'got {}').format(type(data).__name__))

        self._reseed(entropy, data)
        self.reseed_counter = 1

    def _generate(self, count, data):
        ''' Implementations should override this to return random bytes. '''
        raise NotImplementedError()

    def _reseed(self, entropy, data=None):
        ''' Implementations should override this to reseed. '''


class CTRDRBG (DRBG):
    ''' DRBG based on a block cipher in counter mode.

    :param name: A string that describes the cipher and key length to use.
    :type cipher: :class:`str`.
    :param entropy: Refer to :class:`DRBG`.
    :param nonce: Refer to :class:`DRBG`.

    The following ciphers are used:

    name  description          alt. name
    ======  =================  ============
    tdea    3 key triple des   des, 3des
    aes128  AES 128 bit        aes, aes-128
    aes196  AES 196 bit        aes-196
    aes256  AES 256 bit        aes-256
    ======  =================  ============

    Contrary to SP 800-90A all ciphers only support their highest security
    strength setting.
    '''

    def __init__(self, name, entropy, data=None):
        super(CTRDRBG, self).__init__(entropy, data)
        name = name.lower()

        if name not in CIPHERS:
            raise ValueError('Unknown cipher: {}'.format(name))

        if name.startswith('aes'):
            self.cipher = Crypto.Cipher.AES
            self.keylen = int(name[3:])
            self.is_aes = True
            self.max_request_size = 2**13   # bytes or 2**16 bits
            self.outlen = 128

        self.seedlen = (self.outlen + self.keylen) // 8

        if len(entropy) != self.seedlen:
            raise ValueError('Entropy should be exachtly {} bytes long'.format(
                             self.seedlen))

        if data:
            if len(data) > self.seedlen:
                raise ValueError('Only {} bytes of data supported.'.format(
                                 self.seedlen))

            delta = len(entropy) - len(data)

            if delta > 0:
                data = (b'\x00' * delta) + data

            seed_material = strxor(entropy, data)
        else:
            seed_material = entropy

        Key = b'\x00' * (self.keylen // 8)
        V = b'\x00' * (self.outlen // 8)
        self.__key, self.__V = self.__update(seed_material, Key, V)

    def _generate(self, count, data=None):
        if data and len(data) > self.seedlen:
            raise ValueError('Too much data.')

        if data:
            data += b'\x00' * (self.seedlen - len(data))
            self.__key, self.__V = self.__update(data, self.__key,
                                                 self.__V)

        temp = b''
        K, V = self.__key, self.__V

        while len(temp) < count:
            V = long2bytes((bytes2long(V) + 1) % 2**self.outlen)

            if len(V) < self.outlen // 8:
                V = (b'\x00' * (self.outlen // 8 - len(V))) + V

            temp += self.cipher.new(K).encrypt(V)

        self.__key, self.__V = self.__update(data or b'', K, V)
        return temp[:count]

    def _reseed(self, entropy, data=None):
        if data and len(data) > self.seedlen:
            raise ValueError('Too much data.')

        if len(entropy) != self.seedlen:
            raise ValueError('Too much entropy.')

        if data:
            seed_material = strxor(entropy, bytepad(data, self.seedlen))
        else:
            seed_material = entropy

        self.__key, self.__V = self.__update(seed_material, self.__key,
                                             self.__V)

    def __update(self, provided_data, Key, V):
        temp = b''
        cipher = self.cipher.new(Key)

        while len(temp) < self.seedlen:
            V = long2bytes((bytes2long(V) + 1) % 2**self.outlen)
            temp += cipher.encrypt(bytepad(V, self.outlen // 8))

        temp = strxor(temp[:self.seedlen],
                      bytepad(provided_data, self.seedlen))
        Key = temp[:self.keylen // 8]
        V = temp[-self.outlen // 8:]

        return Key, V

    # def _create_df (self):
    #     cipher_const = getattrrr(Crypto.Cipher. self.cipher).new

    #     def BCC (key, data):
    #         assert len(data) % (self.outlen // 8) == 0

    #         bytelen = self.outlen // 8
    #         chaining_value = b'\00' * bytelen
    #         cipher = cipher_const(key)

    #         for i in range(0, len(data) // bytelen, bytelen)
    #             input_block = strxorchaining_value, data[i:i+bytelen]
    #             chaining_value = cipher.encrypt(input_block)

    #         return chaining_value

    #     def df (input_string, no_of_bits_to_return):
    #         L = len(input_string)
    #         N = no_of_bits_to_return // 8
    #         bytelen = self.outlen // 8

    #         S = bytearray((L >> shift) & 0xff for shift in range(3, -1, -1)) +\
    #             bytearray((N >> shift) & 0xff for shift in range(3, -1, -1)) +\
    #             input_string + b'\x80'

    #         if len(S) % bytelen > 0:
    #             S += b'\x00' * (bytelen - len(S) % bytelen)

    #         temp = b'\x00'
    #         i = 0
    #         K = bytearray(range(0, 32))[:bytelen]

    #         while len(temp) < (self.keylen + self.outlen) // 8:
    #             IV = bytearray((i >> shift) & 0xff for shift in range(3, -1, -1)) + \
    #                 (b'\x00' * (bytelen - 4))
    #             temp = temp + BCC(K, (IV + S))
    #             i += 1

    #         K = temp[:self.keylen // 8]
    #         X = temp[self.keylen // 8:(self.keylen + self.outlen) // 8]
    #         temp = b'\x00'

    #         while len(temp) < len(no_of_bits_to_return) // 8:
    #             cipher = Crypto.Cipher.AES.new(K)
    #             X = cipher.encrypt(X)
    #             temp += X

    #         return temp[:no_of_bits_to_return // 8]

    #     return df


class HashDRBG (DRBG):

    def __init__(self, name, entropy, nonce, data=None):
        if name not in DIGESTS:
            raise RuntimeError('Unknown digest {}'.format(name))

        self.digest = getattr(hashlib, name)
        self.max_request_size = 2**16

        self.outlen = 8 * self.digest().digest_size
        self.seedlen = 888 if self.outlen > 256 else 440
        self.security_strength = self.outlen // 2

        super(HashDRBG, self).__init__(entropy, data)

        data = data or b''
        self.__V = self.__df(entropy + nonce + data, self.seedlen)
        self.__C = self.__df(b'\x00' + self.__V, self.seedlen)

    def _reseed(self, entropy, data=None):
        data = data or b''
        self.__V = self.__df(b'\x01' + self.__V + entropy + data, self.seedlen)
        self.__C = self.__df(b'\x00' + self.__V, self.seedlen)

    def _generate(self, count, data=None):
        count_bits = 8 * count

        def hashgen(req, V):
            m = int(math.ceil(count_bits / float(self.outlen)))
            data = V
            w = b''
            for i in range(m):
                w += self.digest(bytepad(data, self.seedlen // 8)).digest()
                data = long2bytes((bytes2long(data) + 1) % 2**self.seedlen)

            return w[:count]

        if data is not None:
            w = self.digest(b'\x02' + self.__V + data).digest()
            V = long2bytes((bytes2long(self.__V) +
                           bytes2long(w)) % 2**self.seedlen)

            if len(V) < self.seedlen // 8:
                delta = self.seedlen // 8 - len(V)
                V = (b'\x00' * delta) + V
        else:
            V = self.__V

        out = hashgen(count_bits, V)
        H = self.digest(b'\x03' + V).digest()
        V = long2bytes((bytes2long(V) +
                       bytes2long(H) +
                       bytes2long(self.__C) +
                       self.reseed_counter
            ) % 2**self.seedlen)

        if len(V) < self.seedlen // 8:
            delta = self.seedlen // 8 - len(V)
            V = (b'\x00' * delta) + V

        self.__V = V
        return out

    def __df(self, input_string, output_bitlen):
        output = b''
        iterations = int(math.ceil(output_bitlen / float(self.outlen)))

        for counter in range(iterations):
            data = bytepad(long2bytes(output_bitlen), 4)
            output += self.digest(bytearray([(counter + 1) % 255]) +
                                  data + input_string).digest()

        return output[:(output_bitlen + 4) // 8]


class HMACDRBG (DRBG):

    def __init__(self, name, entropy, nonce, data=None):
        if name.endswith('hmac'):
            name = name[:-4]

        if name not in DIGESTS:
            raise RuntimeError('Unknown digest {}'.format(name))

        self.digest = getattr(hashlib, name)
        self.max_request_size = 2**16

        self.outlen = 8 * self.digest().digest_size
        self.seedlen = 888 if self.outlen > 256 else 440
        self.security_strength = self.outlen // 2

        super(HMACDRBG, self).__init__(entropy, data)

        outlen = self.outlen // 8
        self.__key, self.__V = self.__update(b'\0' * outlen, b'\x01' * outlen,
                                             entropy, nonce, data)

    def _generate(self, count, data=None):
        if data:
            K, V = self.__update(self.__key, self.__V, data)
        else:
            K, V = self.__key, self.__V

        out = b''

        while len(out) < count:
            V = self._mac(K, V)
            out += V

        self.__key, self.__V = self.__update(K, V, data)

        return out[:count]

    def __update(self, K, V, *data):
        data = [_ for _ in data if _]

        K = self._mac(K, V, b'\x00', *data)
        V = self._mac(K, V)

        if data:
            K = self._mac(K, V, b'\x01', *data)
            V = self._mac(K, V)

        return K, V

    def reseed(self, entropy_input, additional_input=None):
        self.__key, self.__V = self.__update(self.__key, self.__V,
                                             entropy_input, additional_input)

    def _mac(self, K, V, *Vs):
        value = V + b''.join(v for v in Vs if v is not None)
        mac = hmac.new(K, value, digestmod=self.digest)
        return mac.digest()


def new (name, personalization_string=None):
    entropy = os.urandom(128)
    nonce = os.urandom(128)
    drbg = None

    matches = re.match('(sha[0-9]{1,3})(hmac)?', name.lower())

    if matches:
        digest = matches.group(1)
        use_hmac = not matches.group(2) is None

        if digest not in DIGESTS:
            raise ValueError('Unsupported digest {}'.format(digest))

        if use_hmac:
            drbg = HMACDRBG(digest, entropy, nonce, personalization_string)

    return drbg



