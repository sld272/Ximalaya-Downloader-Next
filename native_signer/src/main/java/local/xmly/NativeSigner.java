package local.xmly;

import com.github.unidbg.AndroidEmulator;
import com.github.unidbg.arm.backend.Unicorn2Factory;
import com.github.unidbg.linux.android.AndroidEmulatorBuilder;
import com.github.unidbg.linux.android.AndroidResolver;
import com.github.unidbg.linux.android.dvm.AbstractJni;
import com.github.unidbg.linux.android.dvm.DalvikModule;
import com.github.unidbg.linux.android.dvm.DvmClass;
import com.github.unidbg.linux.android.dvm.DvmObject;
import com.github.unidbg.linux.android.dvm.StringObject;
import com.github.unidbg.linux.android.dvm.VM;
import com.github.unidbg.linux.android.dvm.array.ByteArray;
import com.github.unidbg.linux.android.dvm.array.ArrayObject;
import com.github.unidbg.linux.android.dvm.jni.ProxyDvmObject;
import com.github.unidbg.memory.Memory;
import com.google.gson.Gson;
import com.google.gson.reflect.TypeToken;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.InputStreamReader;
import java.io.ByteArrayInputStream;
import java.io.DataInputStream;
import java.nio.charset.StandardCharsets;
import java.security.KeyFactory;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.security.spec.X509EncodedKeySpec;
import java.security.interfaces.RSAPublicKey;
import java.math.BigInteger;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.TreeMap;
import java.util.Locale;
import javax.crypto.Cipher;
import javax.crypto.spec.SecretKeySpec;

public final class NativeSigner extends AbstractJni implements AutoCloseable {
    private final AndroidEmulator emulator;
    private final VM vm;
    private final DvmClass encryptClass;
    private final DvmObject<?> encryptInstance;
    private final DvmObject<?> context;
    private final DvmObject<?> assetManager;
    private final DvmClass xuidClass;
    private final DvmClass sdkEncryptClass;
    private final DvmObject<?> sdkEncryptInstance;
    private final Gson gson = new Gson();

    NativeSigner(File apk, File libcxx, File library, File xuidLibrary, File encryptLibrary) {
        emulator = AndroidEmulatorBuilder.for64Bit()
                .setProcessName("com.ximalaya.ting.android")
                .addBackendFactory(new Unicorn2Factory(true))
                .build();
        Memory memory = emulator.getMemory();
        memory.setLibraryResolver(new AndroidResolver(23));
        vm = apk == null ? emulator.createDalvikVM() : emulator.createDalvikVM(apk);
        vm.setJni(this);
        vm.setVerbose(Boolean.getBoolean("xmly.verbose"));
        vm.loadLibrary(libcxx, true);
        DalvikModule module = vm.loadLibrary(library, true);
        try { module.callJNI_OnLoad(emulator); } catch (Throwable ignored) { }
        DalvikModule xuidModule = vm.loadLibrary(xuidLibrary, true);
        try { xuidModule.callJNI_OnLoad(emulator); } catch (Throwable ignored) { }
        encryptClass = vm.resolveClass("com/ximalaya/ting/android/loginservice/LoginEncryptUtil");
        encryptInstance = encryptClass.newObject(null);
        context = vm.resolveClass("android/content/ContextWrapper").newObject("com.ximalaya.ting.android");
        assetManager = vm.resolveClass("android/content/res/AssetManager").newObject(null);
        xuidClass = vm.resolveClass("com/ximalaya/xuid/nativelib/NativeLib");
        sdkEncryptClass = vm.resolveClass("com/ximalaya/ting/android/encryptservice/EncryptUtil");
        sdkEncryptInstance = sdkEncryptClass.newObject(null);
        DalvikModule encryptModule = vm.loadLibrary(encryptLibrary, true);
        encryptModule.callJNI_OnLoad(emulator);
    }

    String encryptMobile(String mobile) {
        DvmObject<?> result = encryptInstance.callJniMethodObject(emulator,
                "wwXLkDFrOu(Ljava/lang/String;)Ljava/lang/String;", mobile);
        return result == null ? null : String.valueOf(result.getValue());
    }

    String sign(Map<String, String> values, boolean production) {
        StringBuilder canonical = new StringBuilder();
        new TreeMap<>(values).forEach((key, value) -> canonical.append(key).append('=').append(value).append('&'));
        DvmObject<?> result = encryptInstance.callJniMethodObject(emulator,
                "aXGGIioVBB(Landroid/content/Context;ZLjava/lang/String;)Ljava/lang/String;",
                context, !production, canonical.toString());
        return result == null ? null : String.valueOf(result.getValue());
    }

    private ArrayObject strings(String... values) {
        DvmObject<?>[] objects = new DvmObject<?>[values.length];
        for (int i = 0; i < values.length; i++) objects[i] = new StringObject(vm, values[i] == null ? "" : values[i]);
        return new ArrayObject(objects);
    }

    private void initXuid() {
        int result = xuidClass.callStaticJniMethodInt(emulator, "kCONeLyBJV([Ljava/lang/String;)I",
                strings("com.ximalaya.ting.android", "1.3.15", "9.5.1", "/data/app/com.ximalaya.ting.android/base.apk", "Xiaomi", "M2102J2SC"));
        if (result != 0) throw new IllegalStateException("xuid native init failed: " + result);
    }

    String createXuid(String stableId) {
        initXuid();
        DvmObject<?> value = xuidClass.callStaticJniMethodObject(emulator,
                "dxbPWlbbFU([Ljava/lang/String;)Ljava/lang/String;", strings("U", stableId.replace("-", "")));
        if (value == null) throw new IllegalStateException("native xuid returned null");
        return String.valueOf(value.getValue());
    }

    String ticket(String attr, String xuid) {
        initXuid();
        DvmObject<?> value = xuidClass.callStaticJniMethodObject(emulator,
                "vDMzsjQFqU([Ljava/lang/String;)Ljava/lang/String;", strings(attr, xuid));
        if (value == null) throw new IllegalStateException("native ticket returned null");
        return String.valueOf(value.getValue());
    }

    String decryptDownload(String value, int version) {
        if (value == null || value.isBlank() || value.startsWith("http://") || value.startsWith("https://")) return value;
        if (version == 2) {
            DvmObject<?> result = sdkEncryptInstance.callJniMethodObject(emulator,
                    "cYWOoJESuO(Landroid/content/Context;Ljava/lang/String;)Ljava/lang/String;", context, value);
            return result == null ? null : String.valueOf(result.getValue());
        }
        DvmObject<?> keyValue = sdkEncryptInstance.callJniMethodObject(emulator,
                "CduekLxHQQ(Landroid/content/Context;Ljava/lang/String;)Ljava/lang/String;", context, "play_url_key");
        if (keyValue == null) throw new IllegalStateException("native play_url_key returned null");
        byte[] key = hex(String.valueOf(keyValue.getValue()));
        byte[] encrypted = Base64.getUrlDecoder().decode(padBase64(value));
        try {
            Cipher cipher = Cipher.getInstance("AES/ECB/PKCS5Padding");
            cipher.init(Cipher.DECRYPT_MODE, new SecretKeySpec(key, "AES"));
            return new String(cipher.doFinal(encrypted), StandardCharsets.UTF_8);
        } catch (Exception error) {
            throw new IllegalStateException("download URL AES decrypt failed", error);
        }
    }

    String nativeSecret(String name) {
        DvmObject<?> result = sdkEncryptInstance.callJniMethodObject(emulator,
                "CduekLxHQQ(Landroid/content/Context;Ljava/lang/String;)Ljava/lang/String;", context, name);
        return result == null ? null : String.valueOf(result.getValue());
    }

    String decryptWithKey(String value, String hexKey) throws Exception {
        byte[] encrypted = Base64.getUrlDecoder().decode(padBase64(value));
        Cipher cipher = Cipher.getInstance("AES/ECB/PKCS5Padding");
        cipher.init(Cipher.DECRYPT_MODE, new SecretKeySpec(hex(hexKey), "AES"));
        return new String(cipher.doFinal(encrypted), StandardCharsets.UTF_8);
    }

    private static String padBase64(String value) {
        return value + "=".repeat((4 - value.length() % 4) % 4);
    }

    private static byte[] hex(String value) {
        if ((value.length() & 1) != 0) throw new IllegalArgumentException("odd hex key length");
        byte[] result = new byte[value.length() / 2];
        for (int i = 0; i < result.length; i++)
            result[i] = (byte) Integer.parseInt(value.substring(i * 2, i * 2 + 2), 16);
        return result;
    }

    private Map<String, Object> handle(Map<String, Object> request) {
        Map<String, Object> response = new LinkedHashMap<>();
        try {
            String op = String.valueOf(request.get("op"));
            if ("ping".equals(op)) {
                response.put("ok", true); response.put("engine", "unidbg-arm64");
            } else if ("encryptMobile".equals(op)) {
                String value = encryptMobile(String.valueOf(request.get("mobile")));
                if (value == null || value.isBlank()) throw new IllegalStateException("native encryptMobile returned empty result");
                response.put("ok", true); response.put("value", value);
            } else if ("sign".equals(op)) {
                Map<String, String> values = gson.fromJson(gson.toJson(request.get("values")), new TypeToken<Map<String, String>>(){}.getType());
                String value = sign(values, !Boolean.FALSE.equals(request.get("production")));
                if (value == null || value.isBlank()) throw new IllegalStateException("native sign returned empty result");
                response.put("ok", true); response.put("value", value);
            } else if ("createXuid".equals(op)) {
                String value = createXuid(String.valueOf(request.get("stableId")));
                response.put("ok", true); response.put("value", value);
            } else if ("ticket".equals(op)) {
                String value = ticket(String.valueOf(request.get("attr")), String.valueOf(request.get("xuid")));
                response.put("ok", true); response.put("value", value);
            } else if ("decryptDownload".equals(op)) {
                String value = decryptDownload(String.valueOf(request.get("value")), ((Number) request.get("version")).intValue());
                if (value == null || value.isBlank()) throw new IllegalStateException("native decryptDownload returned empty result");
                response.put("ok", true); response.put("value", value);
            } else if ("nativeSecret".equals(op)) {
                response.put("ok", true); response.put("value", nativeSecret(String.valueOf(request.get("name"))));
            } else if ("decryptWithKey".equals(op)) {
                response.put("ok", true); response.put("value", decryptWithKey(String.valueOf(request.get("value")), String.valueOf(request.get("key"))));
            } else throw new IllegalArgumentException("unknown op: " + op);
        } catch (Throwable error) {
            response.put("ok", false); response.put("error", error.toString());
            if (error.getStackTrace().length > 0) response.put("at", error.getStackTrace()[0].toString());
        }
        return response;
    }

    private DvmObject<?> proxy(Object value) { return ProxyDvmObject.createObject(vm, value); }

    @Override public DvmObject<?> callStaticObjectMethod(com.github.unidbg.linux.android.dvm.BaseVM baseVm, DvmClass dvmClass, String signature, com.github.unidbg.linux.android.dvm.VarArg args) {
        try {
            if ("java/security/KeyFactory->getInstance(Ljava/lang/String;)Ljava/security/KeyFactory;".equals(signature))
                return proxy(KeyFactory.getInstance(String.valueOf(args.<DvmObject<?>>getObjectArg(0).getValue())));
            if ("javax/crypto/Cipher->getInstance(Ljava/lang/String;)Ljavax/crypto/Cipher;".equals(signature))
                return proxy(Cipher.getInstance(String.valueOf(args.<DvmObject<?>>getObjectArg(0).getValue())));
            if ("java/security/MessageDigest->getInstance(Ljava/lang/String;)Ljava/security/MessageDigest;".equals(signature))
                return proxy(MessageDigest.getInstance(String.valueOf(args.<DvmObject<?>>getObjectArg(0).getValue())));
            if ("java/lang/Integer->toHexString(I)Ljava/lang/String;".equals(signature))
                return new StringObject(vm, Integer.toHexString(args.getIntArg(0)));
            if ("android/util/Base64->decode(Ljava/lang/String;I)[B".equals(signature)) {
                String value = String.valueOf(args.<DvmObject<?>>getObjectArg(0).getValue()).replaceAll("\\s", "");
                return new ByteArray(vm, Base64.getDecoder().decode(value));
            }
            if ("android/util/Base64->encodeToString([BI)Ljava/lang/String;".equals(signature)) {
                String value = Base64.getEncoder().encodeToString(args.<ByteArray>getObjectArg(0).getValue());
                return new StringObject(vm, value);
            }
            return super.callStaticObjectMethod(baseVm, dvmClass, signature, args);
        } catch (Exception error) { throw new IllegalStateException(signature, error); }
    }

    @Override public boolean callStaticBooleanMethod(com.github.unidbg.linux.android.dvm.BaseVM baseVm, DvmClass dvmClass, String signature, com.github.unidbg.linux.android.dvm.VarArg args) {
        if ("com/ximalaya/ting/android/host/manager/configurecenter/ConfigureCenterUtil->shalledCheckDevice()Z".equals(signature))
            return false;
        return super.callStaticBooleanMethod(baseVm, dvmClass, signature, args);
    }

    @Override public DvmObject<?> newObject(com.github.unidbg.linux.android.dvm.BaseVM baseVm, DvmClass dvmClass, String signature, com.github.unidbg.linux.android.dvm.VarArg args) {
        if ("java/security/spec/X509EncodedKeySpec-><init>([B)V".equals(signature))
            return proxy(new X509EncodedKeySpec(args.<ByteArray>getObjectArg(0).getValue()));
        if ("javax/crypto/spec/SecretKeySpec-><init>([BLjava/lang/String;)V".equals(signature) ||
                "<init>([BLjava/lang/String;)V".equals(signature))
            return proxy(new SecretKeySpec(args.<ByteArray>getObjectArg(0).getValue(),
                    String.valueOf(args.<DvmObject<?>>getObjectArg(1).getValue())));
        if ("java/io/ByteArrayOutputStream-><init>()V".equals(signature)) return proxy(new ByteArrayOutputStream());
        if ("java/security/SecureRandom-><init>()V".equals(signature) || "<init>()V".equals(signature) && "java/security/SecureRandom".equals(dvmClass.getClassName()))
            return proxy(new SecureRandom());
        if ("java/lang/StringBuilder-><init>()V".equals(signature)) return proxy(new StringBuilder());
        if ("java/io/DataInputStream-><init>(Ljava/io/InputStream;)V".equals(signature))
            return proxy(new DataInputStream((java.io.InputStream) args.<DvmObject<?>>getObjectArg(0).getValue()));
        return super.newObject(baseVm, dvmClass, signature, args);
    }

    @Override public DvmObject<?> callObjectMethod(com.github.unidbg.linux.android.dvm.BaseVM baseVm, DvmObject<?> object, String signature, com.github.unidbg.linux.android.dvm.VarArg args) {
        if ((signature.startsWith("android/content/Context->") || signature.startsWith("android/content/ContextWrapper->")) && signature.endsWith("getPackageName()Ljava/lang/String;"))
            return new StringObject(vm, "com.ximalaya.ting.android");
        if ((signature.startsWith("android/content/Context->") || signature.startsWith("android/content/ContextWrapper->")) && signature.endsWith("getApplicationContext()Landroid/content/Context;"))
            return context;
        if ((signature.startsWith("android/content/Context->") || signature.startsWith("android/content/ContextWrapper->")) && signature.endsWith("getAssets()Landroid/content/res/AssetManager;"))
            return assetManager;
        if ("android/content/res/AssetManager->open(Ljava/lang/String;)Ljava/io/InputStream;".equals(signature)) {
            String name = String.valueOf(args.<DvmObject<?>>getObjectArg(0).getValue());
            System.err.println("native asset open: " + name);
            File file = new File(System.getProperty("xmly.asset.dir", "assets"), name);
            try {
                return proxy(new ByteArrayInputStream(file.isFile() ? java.nio.file.Files.readAllBytes(file.toPath()) : new byte[0]));
            } catch (Exception error) { throw new IllegalStateException(name, error); }
        }
        try {
            if ("java/security/KeyFactory->generatePublic(Ljava/security/spec/KeySpec;)Ljava/security/PublicKey;".equals(signature))
                return proxy(((KeyFactory) object.getValue()).generatePublic((X509EncodedKeySpec) args.<DvmObject<?>>getObjectArg(0).getValue()));
            if ("sun/security/rsa/RSAPublicKeyImpl->getModulus()Ljava/math/BigInteger;".equals(signature) ||
                "java/security/interfaces/RSAPublicKey->getModulus()Ljava/math/BigInteger;".equals(signature))
                return proxy(((RSAPublicKey) object.getValue()).getModulus());
            if ("sun/security/rsa/RSAPublicKeyImpl->getPublicExponent()Ljava/math/BigInteger;".equals(signature) ||
                "java/security/interfaces/RSAPublicKey->getPublicExponent()Ljava/math/BigInteger;".equals(signature))
                return proxy(((RSAPublicKey) object.getValue()).getPublicExponent());
            if ("java/math/BigInteger->toByteArray()[B".equals(signature))
                return new ByteArray(vm, ((BigInteger) object.getValue()).toByteArray());
            if ("java/io/ByteArrayOutputStream->toByteArray()[B".equals(signature))
                return new ByteArray(vm, ((ByteArrayOutputStream) object.getValue()).toByteArray());
            if ("java/lang/String->toUpperCase()Ljava/lang/String;".equals(signature))
                return new StringObject(vm, String.valueOf(object.getValue()).toUpperCase(Locale.ROOT));
            if ("java/lang/StringBuilder->append(Ljava/lang/String;)Ljava/lang/StringBuilder;".equals(signature)) {
                ((StringBuilder) object.getValue()).append(args.<DvmObject<?>>getObjectArg(0).getValue());
                return object;
            }
            if ("java/lang/StringBuilder->append(I)Ljava/lang/StringBuilder;".equals(signature)) {
                ((StringBuilder) object.getValue()).append(args.getIntArg(0));
                return object;
            }
            if ("java/lang/StringBuilder->toString()Ljava/lang/String;".equals(signature))
                return new StringObject(vm, object.getValue().toString());
            if ("java/security/MessageDigest->digest([B)[B".equals(signature))
                return new ByteArray(vm, ((MessageDigest) object.getValue()).digest(args.<ByteArray>getObjectArg(0).getValue()));
            if ("java/security/MessageDigest->digest()[B".equals(signature))
                return new ByteArray(vm, ((MessageDigest) object.getValue()).digest());
            if ("javax/crypto/Cipher->doFinal([B)[B".equals(signature))
                return new ByteArray(vm, ((Cipher) object.getValue()).doFinal(args.<ByteArray>getObjectArg(0).getValue()));
            if ("javax/crypto/Cipher->doFinal([BII)[B".equals(signature))
                return new ByteArray(vm, ((Cipher) object.getValue()).doFinal(args.<ByteArray>getObjectArg(0).getValue(), args.getIntArg(1), args.getIntArg(2)));
        } catch (Exception error) { throw new IllegalStateException(signature, error); }
        return super.callObjectMethod(baseVm, object, signature, args);
    }

    @Override public void callVoidMethod(com.github.unidbg.linux.android.dvm.BaseVM baseVm, DvmObject<?> object, String signature, com.github.unidbg.linux.android.dvm.VarArg args) {
        try {
            if ("javax/crypto/Cipher->init(ILjava/security/Key;)V".equals(signature)) {
                ((Cipher) object.getValue()).init(args.getIntArg(0), (java.security.Key) args.<DvmObject<?>>getObjectArg(1).getValue());
                return;
            }
            if ("java/io/ByteArrayOutputStream->write([BII)V".equals(signature)) {
                ((ByteArrayOutputStream) object.getValue()).write(args.<ByteArray>getObjectArg(0).getValue(), args.getIntArg(1), args.getIntArg(2));
                return;
            }
            if ("java/io/ByteArrayOutputStream->write([B)V".equals(signature)) {
                ((ByteArrayOutputStream) object.getValue()).writeBytes(args.<ByteArray>getObjectArg(0).getValue());
                return;
            }
            if ("java/io/ByteArrayOutputStream->close()V".equals(signature)) return;
            if ("java/security/MessageDigest->update([B)V".equals(signature)) {
                ((MessageDigest) object.getValue()).update(args.<ByteArray>getObjectArg(0).getValue());
                return;
            }
            if ("java/io/DataInputStream->reset()V".equals(signature)) {
                ((DataInputStream) object.getValue()).reset();
                return;
            }
            if ("java/io/DataInputStream->mark(I)V".equals(signature)) {
                ((DataInputStream) object.getValue()).mark(args.getIntArg(0));
                return;
            }
            if ("java/io/DataInputStream->close()V".equals(signature)) return;
            if ("java/io/DataInputStream->readFully([B)V".equals(signature)) {
                ((DataInputStream) object.getValue()).readFully(args.<ByteArray>getObjectArg(0).getValue());
                return;
            }
            if ("java/io/DataInputStream->readFully([BII)V".equals(signature)) {
                ((DataInputStream) object.getValue()).readFully(args.<ByteArray>getObjectArg(0).getValue(), args.getIntArg(1), args.getIntArg(2));
                return;
            }
        } catch (Exception error) { throw new IllegalStateException(signature, error); }
        super.callVoidMethod(baseVm, object, signature, args);
    }

    @Override public int callIntMethod(com.github.unidbg.linux.android.dvm.BaseVM baseVm, DvmObject<?> object, String signature, com.github.unidbg.linux.android.dvm.VarArg args) {
        if ("java/math/BigInteger->bitLength()I".equals(signature)) return ((BigInteger) object.getValue()).bitLength();
        if ("java/lang/String->length()I".equals(signature)) return String.valueOf(object.getValue()).length();
        try {
            if ("java/io/DataInputStream->readInt()I".equals(signature)) return ((DataInputStream) object.getValue()).readInt();
            if ("java/io/DataInputStream->readUnsignedShort()I".equals(signature)) return ((DataInputStream) object.getValue()).readUnsignedShort();
            if ("java/io/DataInputStream->read([B)I".equals(signature)) return ((DataInputStream) object.getValue()).read(args.<ByteArray>getObjectArg(0).getValue());
            if ("java/io/DataInputStream->available()I".equals(signature)) return ((DataInputStream) object.getValue()).available();
            if ("java/io/DataInputStream->skipBytes(I)I".equals(signature)) return ((DataInputStream) object.getValue()).skipBytes(args.getIntArg(0));
        } catch (Exception error) { throw new IllegalStateException(signature, error); }
        return super.callIntMethod(baseVm, object, signature, args);
    }

    @Override public void close() throws Exception { emulator.close(); }

    public static void main(String[] args) throws Exception {
        if (args.length != 4 && args.length != 5) throw new IllegalArgumentException("usage: NativeSigner [APK] LIBCXX LOGIN_SO XUID_SO ENCRYPT_SO");
        File apk = args.length == 5 ? new File(args[0]) : null;
        int offset = args.length == 5 ? 1 : 0;
        try (NativeSigner signer = new NativeSigner(apk, new File(args[offset]), new File(args[offset + 1]), new File(args[offset + 2]), new File(args[offset + 3]));
             BufferedReader reader = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) {
                Map<String, Object> request = signer.gson.fromJson(line, new TypeToken<Map<String, Object>>(){}.getType());
                System.out.println(signer.gson.toJson(signer.handle(request)));
                System.out.flush();
            }
        }
    }
}
