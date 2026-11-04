package kr.aiv2g.supporter;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.view.Gravity;
import android.webkit.CookieManager;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.TextView;

/**
 * 서포터즈 웹앱(/app)을 전체 화면 WebView로 띄운다.
 * - 고유 User-Agent를 보내 ngrok 무료 주소의 경고 화면을 건너뛴다(브라우저가 아닌 요청에는 경고를 띄우지 않음).
 * - 서비스 주소 밖의 링크는 기기 브라우저로 연다.
 * - 뒤로 가기는 웹 화면 안에서 먼저 처리한다.
 */
public class MainActivity extends Activity {
    private WebView web;
    private TextView offline;
    private String host;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        host = Uri.parse(BuildConfig.START_URL).getHost();

        FrameLayout root = new FrameLayout(this);
        web = new WebView(this);
        offline = new TextView(this);
        offline.setText(R.string.offline);
        offline.setGravity(Gravity.CENTER);
        offline.setTextColor(Color.parseColor("#4A5875"));
        offline.setTextSize(16);
        offline.setBackgroundColor(Color.parseColor("#F3F6FB"));
        offline.setVisibility(TextView.GONE);
        offline.setOnClickListener(v -> { offline.setVisibility(TextView.GONE); web.reload(); });
        root.addView(web, new FrameLayout.LayoutParams(-1, -1));
        root.addView(offline, new FrameLayout.LayoutParams(-1, -1));
        setContentView(root);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setUserAgentString(BuildConfig.USER_AGENT);
        s.setAllowFileAccess(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        CookieManager.getInstance().setAcceptCookie(true);

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if (host.equals(uri.getHost())) return false;          // 서비스 화면은 앱 안에서
                try { startActivity(new Intent(Intent.ACTION_VIEW, uri)); } catch (Exception ignored) { }
                return true;                                            // 그 밖의 주소는 기기 브라우저로
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) offline.setVisibility(TextView.VISIBLE);
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                CookieManager.getInstance().flush();                   // 로그인 상태를 앱을 닫아도 유지
            }
        });

        if (savedInstanceState != null) web.restoreState(savedInstanceState);
        else web.loadUrl(BuildConfig.START_URL);
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        web.saveState(outState);
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }
}
