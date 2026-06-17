//! Vowel counting.

/// Count the vowels (a, e, i, o, u) in `s`, case-insensitively.
///
/// Bug: the match arm for `'u'` is missing, so `count_vowels` under-counts any
/// string containing the letter 'u'. Add the missing vowel.
pub fn count_vowels(s: &str) -> usize {
    let mut n = 0;
    for c in s.chars() {
        match c.to_ascii_lowercase() {
            'a' | 'e' | 'i' | 'o' => n += 1,
            _ => {}
        }
    }
    n
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn counts_all_five_vowels() {
        assert_eq!(count_vowels("aeiou"), 5);
    }

    #[test]
    fn counts_u_in_words() {
        // U, u, i, u  => 4 vowels
        assert_eq!(count_vowels("Ununium"), 4);
    }

    #[test]
    fn ignores_consonants() {
        assert_eq!(count_vowels("rhythm"), 0);
    }
}
