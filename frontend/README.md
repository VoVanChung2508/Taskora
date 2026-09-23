# Flowie Frontend (Next.js)

Giao diện web cho Flowie, dùng App Router + TypeScript. Gọi backend Go qua
REST và dùng session cookie (httpOnly) do luồng Azure AD SSO cấp.

## Chạy dev

```bash
cp .env.local.example .env.local
npm install
npm run dev                        # http://localhost:3000 hoặc http://YOUR_LAN_IP:3000
```

Frontend và backend đều bind `0.0.0.0`, vì vậy thiết bị khác trong cùng mạng
LAN có thể truy cập bằng IP LAN của máy chủ, ví dụ `http://192.168.1.23:3000`.
Backend chạy ở port `8081` (xem `../Backend`). Khi mở frontend qua LAN,
API client tự đổi `localhost:8081` thành hostname hiện tại, ví dụ
`192.168.1.23:8081`. Đăng nhập cần
Azure AD được cấu hình ở backend (xem `../docs/azure-sharepoint-setup.md`).

## Cấu trúc

```
src/
├── app/
│   ├── page.tsx                  # Login + danh sách workspace
│   ├── workspaces/[id]/page.tsx  # Danh sách + tạo project
│   └── projects/[id]/page.tsx    # Kanban board của task
├── components/TopBar.tsx
└── lib/api.ts                    # API client (credentials: include)
```

## Phiên bản & bảo mật

- Next.js 16 + React 19 (bản mới nhất tại thời điểm scaffold).
- `npm audit` còn 2 cảnh báo transitive từ `next`: `postcss` và `sharp`
  (CVE 2026 mới, chưa có bản vá trong Next mới nhất). Đây là dep build-time /
  image-optimization; `audit fix --force` sẽ downgrade Next và tái xuất hiện
  các CVE nặng hơn, nên **không** chạy nó. Theo dõi bản Next mới để nâng cấp.
