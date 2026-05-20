import { redirect } from 'next/navigation';

export default function MeSessionsRedirect() {
  redirect('/account/sessions');
}
