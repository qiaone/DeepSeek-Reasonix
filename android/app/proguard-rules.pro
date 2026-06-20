# Add project specific ProGuard rules here.
# The Go binary is in assets, not code, so no special rules needed.

-keepattributes *Annotation*

# Keep the Service so the system can instantiate it
-keep class com.reasonix.app.ReasonixService { *; }
-keep class com.reasonix.app.ReasonixApp { *; }

# WebView JavaScript interface (if added later)
-keepclassmembers class * {
    @android.webkit.JavascriptInterface <methods>;
}
