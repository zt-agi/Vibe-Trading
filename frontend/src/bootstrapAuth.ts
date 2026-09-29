// ZT add-on: one-click web sign-in (bin\start_vt_web.ps1 opens /#vt_key=<key>).
// main.tsx imports this module first, so the key leaves the URL (replaceState)
// before i18n, the router or anything else reads location.
import { consumeApiAuthKeyFromFragment } from "@/lib/apiAuth";

consumeApiAuthKeyFromFragment();
