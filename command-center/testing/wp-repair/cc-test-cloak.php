<?php
/**
 * Plugin Name: CC test cloak (disposable test fixture)
 * Description: Imitates common malware for the Command Center's tests: redirects search
 *              visitors and Googlebot to a non-existent spam domain and prints an
 *              obfuscated (harmless) script. Only ever installed on the disposable test site.
 */
add_action('init', function () {
    $ua = $_SERVER['HTTP_USER_AGENT'] ?? '';
    $ref = $_SERVER['HTTP_REFERER'] ?? '';
    if (stripos($ua, 'Googlebot') !== false || stripos($ref, 'google.') !== false) {
        wp_redirect('https://pharma-spam.invalid/', 301);
        exit;
    }
});
add_action('wp_head', function () {
    echo "<script>eval(atob('Y29uc29sZS5sb2coImNjLXRlc3QiKQ=='))</script>\n";
});
