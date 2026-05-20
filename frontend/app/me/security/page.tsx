import { redirect } from 'next/navigation';

export default function MeSecurityRedirect() {
  redirect('/account/security');
}
