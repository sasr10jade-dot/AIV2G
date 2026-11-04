# AI-V2G 서포터즈 안드로이드 앱

서포터즈 웹앱(`/app`)을 앱 안 WebView로 띄우는 작은 앱(약 40KB)이다. 화면·기능은 웹과 같고, 새 버전은 웹 배포만으로 반영된다(APK를 다시 만들 필요 없음).

- 패키지: `kr.aiv2g.supporter` / 시작 주소: `app/build.gradle`의 `START_URL`
- 고유 User-Agent(`AIV2G-Supporter-App/버전 (Android)`)를 보낸다. ngrok 무료 주소는 브라우저가 아닌 요청에 경고 화면을 띄우지 않으므로,
  앱에서는 'You are about to visit' 화면이 나오지 않는다. 주소창도 없다(WebView).
  - 처음에는 크롬에 맡기는 TWA(Bubblewrap)로 만들었으나, 무료 ngrok 경고 화면과 앱-웹 인증 실패로 주소창이 보여 WebView로 바꿨다(`twa/`는 기록용).
- 서비스 주소 밖의 링크는 기기 브라우저로 열고, 뒤로 가기는 웹 화면 안에서 먼저 처리한다. 로그인 쿠키는 앱을 닫아도 유지된다.
- 서명 키: `C:\Users\Jade\AIV2G-signing\aiv2g-release.jks` (비밀번호는 같은 폴더 `signing-info.txt`) — 저장소에 올리지 않는다.
  잃어버리면 같은 앱으로 업데이트할 수 없다.
- 배포: 서명된 APK를 서버의 `downloads/AIV2G-supporter-버전.apk`로 두면 `/download/AIV2G-supporter.apk`와 설치 안내(`/install`)에서 내려받는다.

## 서비스 주소가 바뀌면
`app/build.gradle`의 `START_URL`을 고치고 `versionCode`를 1 올려 다시 빌드한다(설치된 앱은 새 APK로 덮어 설치).

## 빌드(Windows)
경로에 한글이 있으면 Gradle이 실패하므로 영문 경로에 복사해 빌드한다. 필요: JDK 17, Android SDK(`C:\Users\Jade\android-sdk`).

```powershell
Copy-Item -Recurse -Force android C:\Users\Jade\AIV2G-webview
Set-Location C:\Users\Jade\AIV2G-webview
Remove-Item Env:NoDefaultCurrentDirectoryInExePath -ErrorAction SilentlyContinue   # 일부 실행 환경에서 gradlew.bat을 막는 설정
$env:JAVA_HOME = 'C:\Program Files\Microsoft\jdk-17.0.20.8-hotspot'
Set-Content local.properties 'sdk.dir=C\:\\Users\\Jade\\android-sdk' -Encoding ascii
.\gradlew.bat assembleRelease
# 서명(비밀번호는 환경변수로만 넘긴다 — 명령줄·로그에 남기지 않기)
$env:KS = '<signing-info.txt의 store_password>'
& 'C:\Program Files\Microsoft\jdk-17.0.20.8-hotspot\bin\java.exe' -jar C:\Users\Jade\android-sdk\build-tools\36.1.0\lib\apksigner.jar sign `
  --ks C:\Users\Jade\AIV2G-signing\aiv2g-release.jks --ks-key-alias aiv2g --ks-pass env:KS --key-pass env:KS `
  --out AIV2G-supporter-버전.apk app\build\outputs\apk\release\app-release-unsigned.apk
```
