# AI-V2G 서포터즈 안드로이드 앱(TWA)

서포터즈 앱(PWA, `/app`)을 안드로이드 설치 파일(APK)로 감싼 것이다. 화면·기능은 웹과 같고, 새 버전은 웹 배포만으로 반영된다(APK를 다시 만들 필요 없음).

- 패키지: `kr.aiv2g.supporter` / 주소: `https://stem-shifter-selected.ngrok-free.dev/app`
- 서명 키: `C:\Users\Jade\AIV2G-signing\aiv2g-release.jks` (비밀번호는 같은 폴더 `signing-info.txt`) — 저장소에 올리지 않는다. 잃어버리면 같은 앱으로 업데이트할 수 없다.
- 웹 인증: 서버의 `/.well-known/assetlinks.json`(값: `config/assetlinks.json`)이 서명 키 지문과 같아야 주소창 없이 전체 화면으로 열린다.
- 배포: 서명된 APK를 서버의 `downloads/AIV2G-supporter-버전.apk`로 두면 `/download/AIV2G-supporter.apk`와 설치 안내(`/install`)에서 내려받는다.

## 다시 빌드하기(Windows)
경로에 한글이 있으면 Gradle이 실패하므로 영문 경로(`C:\Users\Jade\AIV2G-android`)에 복사해 빌드한다. 필요: JDK 17, Android SDK(`C:\Users\Jade\android-sdk`), `npm i -g @bubblewrap/cli`.

```powershell
Copy-Item -Recurse -Force android C:\Users\Jade\AIV2G-android
Set-Location C:\Users\Jade\AIV2G-android
'n' | bubblewrap build --skipPwaValidation      # 비밀번호는 BUBBLEWRAP_KEYSTORE_PASSWORD·BUBBLEWRAP_KEY_PASSWORD 환경변수
```
bubblewrap의 서명 단계가 'C:\Program Files' 띄어쓰기로 실패하면 `apksigner.jar sign --ks-pass env:변수명`으로 직접 서명한다.
`twa-manifest.json`의 아이콘·매니페스트 주소가 `127.0.0.1:8766`인 것은 ngrok 무료 경고 화면을 피해 생성 시 로컬 서버에서 받기 위함이다(앱 실행 주소는 `host`).
