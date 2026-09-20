import { redirect } from "next/navigation";

/** Demo/legacy path — merchant dashboard lives at `/`. */
export default function DashboardAliasPage() {
  redirect("/");
}
