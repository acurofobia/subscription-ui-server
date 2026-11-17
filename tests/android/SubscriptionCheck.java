import io.whitelink.android.data.*;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import kotlin.Pair;
import kotlin.ResultKt;
import kotlin.Unit;
import kotlin.coroutines.Continuation;
import kotlin.coroutines.CoroutineContext;
import kotlin.coroutines.EmptyCoroutineContext;
import kotlin.coroutines.intrinsics.IntrinsicsKt;
import kotlin.jvm.functions.Function1;
import org.json.JSONArray;
import org.json.JSONObject;

/** Runs the actual Android repository without Android storage or a VPN tunnel. */
public class SubscriptionCheck {
    private static class Storage implements ProfileStorage {
        private Snapshot snapshot = new Snapshot();
        public String getDeviceId() { return "synthetic-android-compatibility-installation"; }
        public synchronized Snapshot load() { return snapshot; }
        public synchronized void save(Snapshot value) { snapshot = value; }
        public synchronized void clear() { snapshot = new Snapshot(); }
        public synchronized Snapshot mutate(boolean recover, Function1<? super Snapshot, Snapshot> change) {
            snapshot = change.invoke(snapshot);
            return snapshot;
        }
    }

    private static JSONObject refresh(SubscriptionRepository repository, Storage storage, String url) throws Exception {
        var completion = new CompletableFuture<ImportResult>();
        var continuation = new Continuation<ImportResult>() {
            public CoroutineContext getContext() { return EmptyCoroutineContext.INSTANCE; }
            public void resumeWith(Object result) {
                try {
                    ResultKt.throwOnFailure(result);
                    completion.complete((ImportResult) result);
                } catch (Throwable error) { completion.completeExceptionally(error); }
            }
        };
        Object immediate = repository.update(url, continuation);
        if (immediate != IntrinsicsKt.getCOROUTINE_SUSPENDED()) completion.complete((ImportResult) immediate);
        boolean failed = false;
        try { completion.get(20, TimeUnit.SECONDS); }
        catch (java.util.concurrent.ExecutionException error) { failed = true; }
        var snapshot = repository.load();
        var provisioning = snapshot.getProvisioning();
        var decision = storage.deviceAccess(url);
        var row = snapshot.getUiDocument() == null ? null : snapshot.getUiDocument().block("block02");
        return new JSONObject().put("failed", failed).put("complete", snapshot.isComplete())
            .put("vless", snapshot.hasMode(SelectedMode.VLESS)).put("turn", snapshot.hasMode(SelectedMode.TURN))
            .put("nodes", provisioning == null ? 0 : new JSONArray(provisioning.getOutbounds()).length())
            .put("warnings", snapshot.getWarnings().size()).put("title", row == null ? "" : row.getTitle())
            .put("hwid", storage.getSubscriptionHwid())
            .put("state", decision == null ? JSONObject.NULL : decision.getState())
            .put("revision", decision == null ? JSONObject.NULL : decision.getRevision());
    }

    public static void main(String[] args) throws Exception {
        var storage = new Storage();
        var repository = new SubscriptionRepository(storage, true, source -> Unit.INSTANCE,
            () -> new SubscriptionRefreshAttempt(System.currentTimeMillis(), System.nanoTime() / 1_000_000, "test-boot"),
            () -> new Pair<>("15", "Synthetic test device"), value -> Unit.INSTANCE);
        var results = new JSONArray();
        if (args.length == 1) results.put(refresh(repository, storage, args[0]));
        else {
            Path policy = Path.of(args[1]);
            Path ui = Path.of(args[2]);
            String originalUI = Files.readString(ui);
            String[] states = {"allowed", "device_revoked", "allowed", "allowed"};
            int[] revisions = {1, 2, 1, 3};
            for (int i = 0; i < states.length; i++) {
                var decision = new JSONObject().put("state", states[i]).put("revision", revisions[i]);
                Files.writeString(policy, new JSONObject().put("schemaVersion", 1)
                    .put("default", decision).put("devices", new JSONObject()).toString());
                Files.writeString(ui, i == 1 ? "invalid-ui" : originalUI);
                results.put(refresh(repository, storage, args[0]));
            }
        }
        System.out.println(results);
    }
}
