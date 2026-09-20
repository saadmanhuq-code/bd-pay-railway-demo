import { redirect } from "next/navigation";

/** Legacy/demo path — dynamic QR is issued from a merchant offer flow. */
export default function QrAliasPage() {
  redirect("/");
}
