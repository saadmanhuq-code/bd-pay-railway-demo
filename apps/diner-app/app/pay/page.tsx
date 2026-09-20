import { redirect } from "next/navigation";

/** Legacy/demo path — diner checkout lives under /merchants/[id]. */
export default function PayAliasPage() {
  redirect("/");
}
